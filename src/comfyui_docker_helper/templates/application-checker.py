"""Verify application-environment capabilities from narrow expected values."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.machinery
import importlib.metadata as metadata
import importlib.util
import json
import pathlib
import re
import stat
import sys

_PYTORCH_IMPORT_DISTRIBUTIONS = {
    "torch": "torch",
    "torchaudio": "torchaudio",
    "torchvision": "torchvision",
}


def _payload(keys: frozenset[str]) -> dict[str, object]:
    assert len(sys.argv) == 3
    value = json.loads(sys.argv[2])
    assert isinstance(value, dict) and set(value) == keys
    assert all(isinstance(key, str) for key in value)
    return value


def _string(value: object) -> str:
    assert isinstance(value, str) and value
    return value


def _real_directory(value: object) -> pathlib.Path:
    path = value if isinstance(value, pathlib.Path) else pathlib.Path(_string(value))
    path_metadata = path.lstat()
    assert not path.is_symlink()
    assert stat.S_ISDIR(path_metadata.st_mode)
    resolved = path.resolve(strict=True)
    assert resolved == path
    return resolved


def _real_file(path: pathlib.Path, *, parent: pathlib.Path) -> pathlib.Path:
    path_metadata = path.lstat()
    assert not path.is_symlink()
    assert stat.S_ISREG(path_metadata.st_mode)
    resolved = path.resolve(strict=True)
    assert resolved == path and resolved.is_relative_to(parent)
    return resolved


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _verify_package_spec(
    spec: importlib.machinery.ModuleSpec | None,
    *,
    name: str,
    expected_root: pathlib.Path,
    expected_origin: pathlib.Path | None,
) -> tuple[tuple[pathlib.Path, ...], pathlib.Path | None]:
    assert spec is not None and spec.name == name
    assert spec.submodule_search_locations is not None
    raw_locations = tuple(spec.submodule_search_locations)
    assert raw_locations == (str(expected_root),)
    locations = tuple(pathlib.Path(item).resolve(strict=True) for item in raw_locations)
    assert locations == (expected_root,)
    expected_origin_value = None if expected_origin is None else str(expected_origin)
    assert spec.origin == expected_origin_value
    origin = (
        None if spec.origin is None else pathlib.Path(spec.origin).resolve(strict=True)
    )
    assert origin == expected_origin
    return locations, origin


def _expected_package(
    root: pathlib.Path, name: str
) -> tuple[pathlib.Path, pathlib.Path | None]:
    raw = root / pathlib.Path(*name.split("."))
    expected_root = _real_directory(raw)
    assert expected_root.is_relative_to(root)
    initializer = raw / "__init__.py"
    try:
        expected_origin = _real_file(initializer, parent=expected_root)
    except FileNotFoundError:
        expected_origin = None
    return expected_root, expected_origin


def _workspace_launch_path(workspace: pathlib.Path) -> list[str]:
    workspace_entry = str(workspace)
    launch_path = [workspace_entry]
    launch_path.extend(entry for entry in sys.path if entry != workspace_entry)
    assert launch_path[0] == workspace_entry
    assert launch_path.count(workspace_entry) == 1
    return launch_path


def _check_inventory() -> None:
    payload = _payload(frozenset({"distributions"}))
    expected = payload["distributions"]
    assert isinstance(expected, dict)
    assert all(
        isinstance(name, str) and name and isinstance(version, str) and version
        for name, version in expected.items()
    )
    actual = {name: metadata.version(name) for name in expected}
    assert actual == expected, (expected, actual)


def _check_pip() -> None:
    payload = _payload(frozenset({"site_packages", "workspace", "version", "commands"}))
    site_packages = _real_directory(payload["site_packages"])
    workspace = _real_directory(payload["workspace"])
    expected_version = _string(payload["version"])
    commands = payload["commands"]
    assert isinstance(commands, list) and len(commands) == 2
    assert all(isinstance(item, str) and item for item in commands)
    sys.path.insert(0, str(site_packages))
    sys.path.insert(0, str(workspace))

    owners = tuple(
        distribution
        for distribution in metadata.distributions(path=[str(site_packages)])
        if distribution.metadata.get("Name") is not None
        and _normalize_name(distribution.metadata["Name"]) == "pip"
    )
    assert len(owners) == 1
    owner = owners[0]
    assert owner.version == expected_version
    assert pathlib.Path(owner.locate_file("")).resolve(strict=True) == site_packages
    for expected_command in (pathlib.Path(item) for item in commands):
        owned_files = tuple(
            item
            for item in owner.files or ()
            if pathlib.Path(owner.locate_file(item)).resolve(strict=True)
            == expected_command
        )
        assert len(owned_files) == 1
        owned_file = owned_files[0]
        assert owned_file.hash is not None and owned_file.hash.mode == "sha256"
        digest = (
            base64.urlsafe_b64encode(
                hashlib.sha256(expected_command.read_bytes()).digest()
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        assert digest == owned_file.hash.value

    pip_root = _real_directory(site_packages / "pip")
    assert pip_root.is_relative_to(site_packages)
    pip_init = _real_file(pip_root / "__init__.py", parent=pip_root)

    def verify_spec(spec: importlib.machinery.ModuleSpec | None) -> None:
        _verify_package_spec(
            spec,
            name="pip",
            expected_root=pip_root,
            expected_origin=pip_init,
        )

    verify_spec(importlib.util.find_spec("pip"))
    module = importlib.import_module("pip")
    verify_spec(module.__spec__)
    assert pathlib.Path(module.__file__).resolve(strict=True) == pip_init
    assert tuple(
        pathlib.Path(item).resolve(strict=True) for item in module.__path__
    ) == (pip_root,)


def _check_pytorch() -> None:
    payload = _payload(
        frozenset({"site_packages", "workspace", "distributions", "modules"})
    )
    site_packages = _real_directory(payload["site_packages"])
    workspace = _real_directory(payload["workspace"])
    distributions = payload["distributions"]
    modules = payload["modules"]
    assert isinstance(distributions, dict)
    assert all(
        isinstance(name, str)
        and name
        and name == _normalize_name(name)
        and isinstance(version, str)
        and version
        for name, version in distributions.items()
    )
    assert isinstance(modules, dict)
    assert all(
        isinstance(module, str)
        and isinstance(distribution, str)
        and _PYTORCH_IMPORT_DISTRIBUTIONS.get(module) == distribution
        and distribution in distributions
        for module, distribution in modules.items()
    )
    sys.path.insert(0, str(site_packages))
    sys.path.insert(0, str(workspace))

    installed: dict[str, list[metadata.Distribution]] = {}
    for distribution in metadata.distributions(path=[str(site_packages)]):
        raw_name = distribution.metadata.get("Name")
        if raw_name is not None:
            installed.setdefault(_normalize_name(raw_name), []).append(distribution)
    for distribution_name, expected_version in distributions.items():
        owners = installed.get(distribution_name, [])
        assert len(owners) == 1
        owner = owners[0]
        assert owner.version == expected_version
        assert pathlib.Path(owner.locate_file("")).resolve(strict=True) == site_packages

    for module_name, distribution_name in modules.items():
        expected_version = distributions[distribution_name]
        expected_root, expected_init = _expected_package(site_packages, module_name)
        assert expected_init is not None
        module_spec = importlib.util.find_spec(module_name)
        _verify_package_spec(
            module_spec,
            name=module_name,
            expected_root=expected_root,
            expected_origin=expected_init,
        )
        imported = importlib.import_module(module_name)
        assert imported.__version__ == expected_version
        _verify_package_spec(
            imported.__spec__,
            name=module_name,
            expected_root=expected_root,
            expected_origin=expected_init,
        )
        assert pathlib.Path(imported.__file__).resolve(strict=True) == expected_init
        assert tuple(
            pathlib.Path(item).resolve(strict=True) for item in imported.__path__
        ) == (expected_root,)


def _verify_folder_paths_spec(
    spec: importlib.machinery.ModuleSpec | None, folder_paths: pathlib.Path
) -> pathlib.Path:
    assert spec is not None and spec.name == "folder_paths"
    assert spec.origin is not None and spec.origin == str(folder_paths)
    origin = pathlib.Path(spec.origin).resolve(strict=True)
    assert origin == folder_paths
    assert spec.submodule_search_locations is None
    return origin


def _check_comfyui() -> None:
    payload = _payload(frozenset({"workspace"}))
    workspace = _real_directory(payload["workspace"])
    folder_paths = _real_file(workspace / "folder_paths.py", parent=workspace)
    comfy_root, comfy_init = _expected_package(workspace, "comfy")
    launch_path = _workspace_launch_path(workspace)

    folder_paths_origin = _verify_folder_paths_spec(
        importlib.machinery.PathFinder.find_spec("folder_paths", launch_path),
        folder_paths,
    )
    comfy_locations, comfy_origin = _verify_package_spec(
        importlib.machinery.PathFinder.find_spec("comfy", launch_path),
        name="comfy",
        expected_root=comfy_root,
        expected_origin=comfy_init,
    )

    sys.path[:] = launch_path
    folder_paths_module = importlib.import_module("folder_paths")
    imported_folder_paths_origin = _verify_folder_paths_spec(
        folder_paths_module.__spec__, folder_paths
    )
    assert imported_folder_paths_origin == folder_paths_origin
    assert folder_paths_module.__file__ == str(folder_paths)

    comfy_module = importlib.import_module("comfy")
    imported_locations, imported_origin = _verify_package_spec(
        comfy_module.__spec__,
        name="comfy",
        expected_root=comfy_root,
        expected_origin=comfy_init,
    )
    assert imported_locations == comfy_locations
    assert imported_origin == comfy_origin
    expected_comfy_file = None if comfy_init is None else str(comfy_init)
    assert comfy_module.__file__ == expected_comfy_file
    comfy_file = (
        None
        if comfy_module.__file__ is None
        else pathlib.Path(comfy_module.__file__).resolve(strict=True)
    )
    assert comfy_file == comfy_init
    assert tuple(comfy_module.__path__) == (str(comfy_root),)


def _check_manager() -> None:
    payload = _payload(
        frozenset({"version", "workspace", "site_packages", "import_name"})
    )
    expected_version = _string(payload["version"])
    workspace = _real_directory(payload["workspace"])
    site_packages = _real_directory(payload["site_packages"])
    manager_name = _string(payload["import_name"])

    sys.path[:] = [
        item
        for item in sys.path
        if not item or pathlib.Path(item).resolve() not in {workspace, site_packages}
    ]
    sys.path.insert(0, str(site_packages))
    sys.path.insert(0, str(workspace))

    owners = tuple(
        distribution
        for distribution in metadata.distributions(path=[str(site_packages)])
        if distribution.metadata.get("Name") is not None
        and _normalize_name(distribution.metadata["Name"]) == "comfyui-manager"
    )
    assert len(owners) == 1
    assert owners[0].version == expected_version
    assert pathlib.Path(owners[0].locate_file("")).resolve(strict=True) == site_packages

    folder_paths = _real_file(workspace / "folder_paths.py", parent=workspace)
    folder_paths_spec = importlib.util.find_spec("folder_paths")
    assert folder_paths_spec is not None and folder_paths_spec.origin is not None
    assert pathlib.Path(folder_paths_spec.origin).resolve(strict=True) == folder_paths

    for name, root in ((manager_name, site_packages), ("comfy", workspace)):
        expected_root, expected_origin = _expected_package(root, name)
        locations, observed_origin = _verify_package_spec(
            importlib.util.find_spec(name),
            name=name,
            expected_root=expected_root,
            expected_origin=expected_origin,
        )
        imported = importlib.import_module(name)
        imported_locations, imported_origin = _verify_package_spec(
            imported.__spec__,
            name=name,
            expected_root=expected_root,
            expected_origin=expected_origin,
        )
        assert imported_locations == locations
        assert imported_origin == observed_origin
        imported_file = (
            None
            if imported.__file__ is None
            else pathlib.Path(imported.__file__).resolve(strict=True)
        )
        assert imported_file == expected_origin
        assert tuple(
            pathlib.Path(item).resolve(strict=True) for item in imported.__path__
        ) == (expected_root,)


def _check_manager_absent() -> None:
    _payload(frozenset())
    try:
        metadata.version("comfyui-manager")
    except metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError("Manager distribution exists while disabled")
    assert importlib.util.find_spec("comfyui_manager") is None, (
        "Manager import exists while disabled"
    )


def _check_audio() -> None:
    _payload(frozenset())
    import torch
    import torchaudio

    waveform = torch.ones((1, 1600), dtype=torch.float32)
    assert (waveform + 1).sum().item() == 3200
    resampled = torchaudio.functional.resample(waveform, 16000, 8000)
    assert resampled.shape == (1, 800)
    mel = torchaudio.transforms.MelSpectrogram(
        sample_rate=16000,
        n_fft=400,
    )(waveform)
    assert mel.ndim == 3 and mel.shape[0] == 1 and mel.shape[1] > 0


_CHECKS = {
    "inventory": _check_inventory,
    "pip": _check_pip,
    "pytorch": _check_pytorch,
    "comfyui": _check_comfyui,
    "manager": _check_manager,
    "manager-absent": _check_manager_absent,
    "audio": _check_audio,
}


def main() -> None:
    assert len(sys.argv) >= 2
    check = _CHECKS.get(sys.argv[1])
    assert check is not None
    check()


if __name__ == "__main__":
    main()
