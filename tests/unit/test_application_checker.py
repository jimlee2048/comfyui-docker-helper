"""Standalone application checker identity and capability contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from comfyui_docker_helper.application_checkers import APPLICATION_CHECKER_SOURCE


def _application_venv(
    tmp_path: Path, *, import_roots: tuple[Path, ...] = ()
) -> tuple[Path, Path]:
    virtual_env = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(virtual_env)
    site_packages = (
        virtual_env
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    if import_roots:
        site_packages.joinpath("test-import-roots.pth").write_text(
            "".join(f"{os.fspath(path)}\n" for path in import_roots)
        )
    return virtual_env / "bin/python", site_packages


def _run(
    python: Path, capability: str, expected: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            python,
            "-I",
            APPLICATION_CHECKER_SOURCE,
            capability,
            json.dumps(expected, sort_keys=True, separators=(",", ":")),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _distribution(site_packages: Path, name: str, version: str) -> Path:
    metadata = site_packages / f"{name.replace('-', '_')}-{version}.dist-info"
    metadata.mkdir()
    metadata.joinpath("METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    )
    return metadata


def _workspace(tmp_path: Path, *, initialized: bool = True) -> Path:
    workspace = tmp_path / "ComfyUI"
    workspace.mkdir()
    workspace.joinpath("folder_paths.py").write_text("")
    comfy = workspace / "comfy"
    comfy.mkdir()
    if initialized:
        comfy.joinpath("__init__.py").write_text("")
    return workspace


def _pip_fixture(tmp_path: Path) -> tuple[Path, Path, Path, tuple[Path, Path]]:
    workspace = _workspace(tmp_path)
    python, site_packages = _application_venv(tmp_path)
    package = site_packages / "pip"
    package.mkdir()
    package.joinpath("__init__.py").write_text('__version__ = "26.1.2"\n')
    metadata = _distribution(site_packages, "pip", "26.1.2")
    commands = (python.parent / "pip", python.parent / "pip3")
    rows = []
    for command in commands:
        content = f"#!{python}\nprint('pip 26.1.2')\n".encode()
        command.write_bytes(content)
        command.chmod(0o755)
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        rows.append(f"../../../bin/{command.name},sha256={digest},{len(content)}")
    metadata.joinpath("RECORD").write_text("\n".join(rows) + "\n")
    return python, site_packages, workspace, commands


def _pytorch_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict[str, str], dict[str, str]]:
    workspace = _workspace(tmp_path)
    python, site_packages = _application_venv(tmp_path)
    modules = {
        "torch": "2.12.1+cu130",
        "torchaudio": "2.11.0+cu130",
        "torchvision": "0.27.1+cu130",
    }
    for name, version in modules.items():
        package = site_packages / name
        package.mkdir()
        package.joinpath("__init__.py").write_text(f"__version__ = {version!r}\n")
        _distribution(site_packages, name, version)
    distributions = {**modules, "xformers": "0.0.35+cu130"}
    _distribution(site_packages, "xformers", distributions["xformers"])
    import_contracts = {name: name for name in modules}
    return python, site_packages, workspace, distributions, import_contracts


def _manager_fixture(
    tmp_path: Path,
    *,
    manager_initialized: bool = False,
    comfy_initialized: bool = False,
    extra_import_roots: tuple[Path, ...] = (),
) -> tuple[Path, Path, Path, Path]:
    workspace = _workspace(tmp_path, initialized=comfy_initialized)
    python, site_packages = _application_venv(
        tmp_path, import_roots=(workspace, *extra_import_roots)
    )
    manager = site_packages / "comfyui_manager"
    manager.mkdir()
    if manager_initialized:
        manager.joinpath("__init__.py").write_text("")
    _distribution(site_packages, "comfyui-manager", "4.0.5")
    return python, site_packages, workspace, manager


def _manager_payload(site_packages: Path, workspace: Path) -> dict[str, str]:
    return {
        "version": "4.0.5",
        "workspace": os.fspath(workspace),
        "site_packages": os.fspath(site_packages),
        "import_name": "comfyui_manager",
    }


def test_checker_is_standalone_linted_python_with_exact_inventory_input(
    tmp_path: Path,
) -> None:
    """Execute the real artifact without cdh or third-party application imports."""
    source = APPLICATION_CHECKER_SOURCE.read_text(encoding="utf-8")
    compile(source, str(APPLICATION_CHECKER_SOURCE), "exec")
    assert "comfyui_docker_helper" not in source
    assert "from packaging" not in source
    python, site_packages = _application_venv(tmp_path)
    _distribution(site_packages, "example-package", "1.2.3+local")

    completed = _run(
        python,
        "inventory",
        {"distributions": {"example-package": "1.2.3+local"}},
    )

    assert completed.returncode == 0, completed.stderr


def test_inventory_rejects_exact_version_drift(tmp_path: Path) -> None:
    """Fail closed when a resolved distribution has a different full version."""
    python, site_packages = _application_venv(tmp_path)
    _distribution(site_packages, "example-package", "1.2.3+other")

    completed = _run(
        python,
        "inventory",
        {"distributions": {"example-package": "1.2.3+expected"}},
    )

    assert completed.returncode != 0


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        ("unknown", {}),
        ("manager-absent", {"unexpected": True}),
        ("inventory", {"distributions": []}),
        (
            "pytorch",
            {
                "site_packages": "/tmp",
                "workspace": "/tmp",
                "distributions": [],
                "modules": [],
            },
        ),
    ],
)
def test_checker_rejects_unknown_or_malformed_typed_inputs(
    tmp_path: Path,
    capability: str,
    expected: object,
) -> None:
    """Reject inputs outside the narrow capability-owned JSON shapes."""
    python, _site_packages = _application_venv(tmp_path)
    assert _run(python, capability, expected).returncode != 0


def test_pip_binds_distribution_module_and_recorded_commands(tmp_path: Path) -> None:
    """Bind pip metadata, import origin, and both command bytes to one owner."""
    python, site_packages, workspace, commands = _pip_fixture(tmp_path)

    completed = _run(
        python,
        "pip",
        {
            "site_packages": os.fspath(site_packages),
            "workspace": os.fspath(workspace),
            "version": "26.1.2",
            "commands": [os.fspath(command) for command in commands],
        },
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "mutation", ["version", "command", "module-symlink", "workspace-shadow"]
)
def test_pip_rejects_identity_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Reject version, command-byte, filesystem, and import-owner substitution."""
    python, site_packages, workspace, commands = _pip_fixture(tmp_path)
    version = "26.1.2"
    if mutation == "version":
        version = "26.1.3"
    elif mutation == "command":
        commands[0].write_text(f"#!{python}\nprint('changed')\n")
    elif mutation == "module-symlink":
        package = site_packages / "pip"
        outside = tmp_path / "outside-pip"
        package.rename(outside)
        package.symlink_to(outside, target_is_directory=True)
    else:
        shadow = workspace / "pip"
        shadow.mkdir()
        shadow.joinpath("__init__.py").write_text("")

    completed = _run(
        python,
        "pip",
        {
            "site_packages": os.fspath(site_packages),
            "workspace": os.fspath(workspace),
            "version": version,
            "commands": [os.fspath(command) for command in commands],
        },
    )

    assert completed.returncode != 0


def test_pytorch_binds_all_distributions_and_only_known_imports(
    tmp_path: Path,
) -> None:
    """Bind every direct distribution and stable import to its exact site owner."""
    python, site_packages, workspace, distributions, modules = _pytorch_fixture(
        tmp_path
    )

    completed = _run(
        python,
        "pytorch",
        {
            "site_packages": os.fspath(site_packages),
            "workspace": os.fspath(workspace),
            "distributions": distributions,
            "modules": modules,
        },
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "mutation",
    ["metadata-version", "module-version", "symlink-origin", "workspace-shadow"],
)
def test_pytorch_rejects_version_or_origin_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Reject metadata, runtime-version, filesystem, and search-order drift."""
    python, site_packages, workspace, distributions, modules = _pytorch_fixture(
        tmp_path
    )
    if mutation == "metadata-version":
        distributions["torch"] = "2.12.1+other"
    elif mutation == "module-version":
        site_packages.joinpath("torch/__init__.py").write_text(
            '__version__ = "2.12.1+other"\n'
        )
    elif mutation == "symlink-origin":
        package = site_packages / "torch"
        outside = tmp_path / "outside-torch"
        package.rename(outside)
        package.symlink_to(outside, target_is_directory=True)
    else:
        shadow = workspace / "torch"
        shadow.mkdir()
        shadow.joinpath("__init__.py").write_text('__version__ = "2.12.1+cu130"\n')

    completed = _run(
        python,
        "pytorch",
        {
            "site_packages": os.fspath(site_packages),
            "workspace": os.fspath(workspace),
            "distributions": distributions,
            "modules": modules,
        },
    )

    assert completed.returncode != 0


def test_pytorch_rejects_distribution_only_extra_version_drift(tmp_path: Path) -> None:
    """Fail closed on an extra distribution without inventing an import contract."""
    python, site_packages, workspace, distributions, modules = _pytorch_fixture(
        tmp_path
    )
    distributions["xformers"] = "0.0.36+cu130"

    completed = _run(
        python,
        "pytorch",
        {
            "site_packages": os.fspath(site_packages),
            "workspace": os.fspath(workspace),
            "distributions": distributions,
            "modules": modules,
        },
    )

    assert completed.returncode != 0


@pytest.mark.parametrize("initialized", [False, True], ids=["namespace", "initialized"])
def test_comfyui_accepts_supported_checkout_shapes(
    tmp_path: Path,
    initialized: bool,
) -> None:
    """Accept the supported namespace and initialized ComfyUI package shapes."""
    workspace = _workspace(tmp_path, initialized=initialized)
    python, _site_packages = _application_venv(tmp_path)
    workspace_entry = os.fspath(workspace)
    workspace.joinpath("folder_paths.py").write_text(
        "import sys\n"
        f"assert sys.path[0] == {workspace_entry!r}\n"
        f"assert sys.path.count({workspace_entry!r}) == 1\n"
    )

    completed = _run(python, "comfyui", {"workspace": workspace_entry})

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "workspace",
        "folder-paths",
        "comfy-root",
        "comfy-init",
        "namespace-shadow",
        "regular-shadow",
        "post-import-path",
        "post-import-file",
    ],
)
def test_comfyui_rejects_filesystem_pathfinder_or_post_import_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Fail before or after import when checkout identity leaves its raw owner."""
    initialized = mutation not in {"namespace-shadow", "regular-shadow"}
    workspace = _workspace(tmp_path, initialized=initialized)
    outside = tmp_path / "outside"
    outside.mkdir()
    import_roots: tuple[Path, ...] = ()
    side_effect = tmp_path / "shadow-imported"
    if mutation == "workspace":
        relocated = outside / "ComfyUI"
        workspace.rename(relocated)
        workspace.symlink_to(relocated, target_is_directory=True)
    elif mutation == "folder-paths":
        workspace.joinpath("folder_paths.py").unlink()
        outside.joinpath("folder_paths.py").write_text("")
        workspace.joinpath("folder_paths.py").symlink_to(outside / "folder_paths.py")
    elif mutation == "comfy-root":
        workspace.joinpath("comfy/__init__.py").unlink()
        workspace.joinpath("comfy").rmdir()
        outside.joinpath("comfy").mkdir()
        workspace.joinpath("comfy").symlink_to(
            outside / "comfy", target_is_directory=True
        )
    elif mutation == "comfy-init":
        workspace.joinpath("comfy/__init__.py").unlink()
        outside.joinpath("comfy-init.py").write_text("")
        workspace.joinpath("comfy/__init__.py").symlink_to(outside / "comfy-init.py")
    elif mutation in {"namespace-shadow", "regular-shadow"}:
        shadow = outside / "comfy"
        shadow.mkdir()
        if mutation == "regular-shadow":
            shadow.joinpath("__init__.py").write_text(
                "from pathlib import Path\n"
                f"Path({os.fspath(side_effect)!r}).write_text('bad')\n"
            )
        import_roots = (outside,)
    elif mutation == "post-import-path":
        workspace.joinpath("comfy/__init__.py").write_text(
            "__path__.append('/tmp/comfy-shadow')\n"
        )
    else:
        workspace.joinpath("comfy/__init__.py").write_text(
            "__file__ = '/tmp/comfy-shadow/__init__.py'\n"
        )
    python, _site_packages = _application_venv(tmp_path, import_roots=import_roots)

    completed = _run(python, "comfyui", {"workspace": os.fspath(workspace)})

    assert completed.returncode != 0
    assert not side_effect.exists()


@pytest.mark.parametrize(
    ("manager_initialized", "comfy_initialized"),
    [(False, False), (True, True)],
    ids=["namespaces", "initialized"],
)
def test_manager_accepts_enabled_capability_shapes(
    tmp_path: Path,
    manager_initialized: bool,
    comfy_initialized: bool,
) -> None:
    """Prove enabled Manager metadata plus Manager and ComfyUI import owners."""
    python, site_packages, workspace, _manager = _manager_fixture(
        tmp_path,
        manager_initialized=manager_initialized,
        comfy_initialized=comfy_initialized,
    )

    completed = _run(python, "manager", _manager_payload(site_packages, workspace))

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "mutation",
    ["version", "manager-symlink", "manager-shadow", "comfy-shadow"],
)
def test_manager_rejects_enabled_capability_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Reject Manager metadata or imported capability ownership drift."""
    python, site_packages, workspace, manager = _manager_fixture(tmp_path)
    payload = _manager_payload(site_packages, workspace)
    side_effect = tmp_path / "shadow-imported"
    if mutation == "version":
        payload["version"] = "4.0.6"
    elif mutation == "manager-symlink":
        outside = tmp_path / "outside-manager"
        manager.rename(outside)
        manager.symlink_to(outside, target_is_directory=True)
    elif mutation == "manager-shadow":
        shadow = workspace / "comfyui_manager"
        shadow.mkdir()
        shadow.joinpath("__init__.py").write_text(
            "from pathlib import Path\n"
            f"Path({os.fspath(side_effect)!r}).write_text('bad')\n"
        )
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        shadow = outside / "comfy"
        shadow.mkdir()
        shadow.joinpath("__init__.py").write_text(
            "from pathlib import Path\n"
            f"Path({os.fspath(side_effect)!r}).write_text('bad')\n"
        )
        site_packages.joinpath("extra-shadow.pth").write_text(f"{os.fspath(outside)}\n")

    completed = _run(python, "manager", payload)

    assert completed.returncode != 0
    assert not side_effect.exists()


def test_manager_absence_accepts_clean_environment(tmp_path: Path) -> None:
    """Accept disabled Manager only when distribution and import are both absent."""
    python, _site_packages = _application_venv(tmp_path)
    completed = _run(python, "manager-absent", {})
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("presence", ["distribution", "import"])
def test_manager_absence_rejects_distribution_or_import_presence(
    tmp_path: Path,
    presence: str,
) -> None:
    """Reject either disabled-Manager residue without importing its package."""
    python, site_packages = _application_venv(tmp_path)
    side_effect = tmp_path / "manager-imported"
    if presence == "distribution":
        _distribution(site_packages, "comfyui-manager", "4.0.5")
    else:
        package = site_packages / "comfyui_manager"
        package.mkdir()
        package.joinpath("__init__.py").write_text(
            "from pathlib import Path\n"
            f"Path({os.fspath(side_effect)!r}).write_text('bad')\n"
        )

    completed = _run(python, "manager-absent", {})

    assert completed.returncode != 0
    assert not side_effect.exists()
