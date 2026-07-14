"""Local syntax and cleanup checks for the durable application harness."""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.version import Version
from tests.smoke.test_application_acceptance_live import (
    _COMFY_CLI_BRIDGE_PROBE,
    _COMMON_IMAGE_PROBE,
    _CPU_PROBE,
    _GPU_PROBE,
    _INVENTORY_OBSERVER_SOURCE,
    _MANAGER_FILESYSTEM_PROOF_SOURCE,
    _PYTHON_ROUTING_PROOF_SOURCE,
    _SERVICE_HELPERS,
)


@pytest.mark.parametrize(
    "probe",
    [_COMMON_IMAGE_PROBE, _CPU_PROBE, _GPU_PROBE, _COMFY_CLI_BRIDGE_PROBE],
    ids=["common", "cpu", "gpu", "comfy-cli-bridge"],
)
def test_application_probe_has_valid_posix_shell_and_python_syntax(
    probe: str,
) -> None:
    completed = subprocess.run(
        ["/bin/sh", "-n"],
        input=probe,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    sources = re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|\Z)", probe, flags=re.DOTALL)
    if probe != _COMFY_CLI_BRIDGE_PROBE:
        assert sources
    for index, source in enumerate(sources):
        compile(source, f"<application-probe-{index}>", "exec")


def _service_script(
    tmp_path: Path,
    *,
    log_text: str,
    child_status: int,
    command: str,
) -> str:
    ready_path = tmp_path / "ready"
    pid_path = tmp_path / "pid"
    log_path = tmp_path / "application.log"
    ready = shlex.quote(str(ready_path))
    pid = shlex.quote(str(pid_path))
    log = shlex.quote(str(log_path))
    child_path = tmp_path / "service.py"
    child_path.write_text(
        "import os\n"
        "import pathlib\n"
        "import sys\n"
        "import time\n"
        f"ready = pathlib.Path({str(ready_path)!r})\n"
        f"pid = pathlib.Path({str(pid_path)!r})\n"
        f"log = pathlib.Path({str(log_path)!r})\n"
        "pid.write_text(str(os.getpid()))\n"
        "ready.touch()\n"
        "try:\n"
        "    time.sleep(60)\n"
        "except KeyboardInterrupt:\n"
        f"    log.write_text({log_text!r})\n"
        f"    sys.exit({child_status})\n"
    )
    return (
        "set -eu\n"
        f"application_log={log}\n"
        f"application_python={shlex.quote(sys.executable)}\n"
        f"application_main={shlex.quote(str(child_path))}\n"
        f"{_SERVICE_HELPERS}\n"
        "launch_application\n"
        f"while test ! -e {ready}; do sleep 0.01; done\n"
        f'test "$(cat {pid})" = "$application_pid"\n'
        f"{command}\n"
    )


def test_clean_stop_uses_sigint_requires_zero_and_preserves_log(tmp_path: Path) -> None:
    script = _service_script(
        tmp_path,
        log_text="ready\nStopped server\n",
        child_status=0,
        command='stop_application_cleanly\ntest -z "$application_pid"',
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ready\nStopped server\n"


@pytest.mark.parametrize(
    "log_text,child_status",
    [
        ("ready\n", 0),
        ("Stopped server\n", 2),
        ("Traceback\nStopped server\n", 0),
        ("CUDA out of memory\nStopped server\n", 0),
        ("nodes_audio import warning\nStopped server\n", 0),
        ("prefix Stopped server\n", 0),
        ("Cannot import\ncomfy_extras.nodes_audio\nStopped server\n", 0),
        (
            "WARNING\n/workspace/ComfyUI/comfy_extras/nodes_audio.py\nStopped server\n",
            0,
        ),
        ("comfy_extras/nodes_audio.py\nERROR\nStopped server\n", 0),
    ],
)
def test_clean_stop_rejects_incomplete_or_failed_shutdown(
    tmp_path: Path,
    log_text: str,
    child_status: int,
) -> None:
    completed = subprocess.run(
        [
            "/bin/sh",
            "-c",
            _service_script(
                tmp_path,
                log_text=log_text,
                child_status=child_status,
                command="stop_application_cleanly",
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )

    assert completed.returncode != 0
    assert completed.stdout == log_text


def test_emergency_cleanup_preserves_original_failure_status(tmp_path: Path) -> None:
    script = _service_script(
        tmp_path,
        log_text="cleanup log\n",
        child_status=0,
        command="exit 7",
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )

    assert completed.returncode == 7, completed.stderr
    assert completed.stdout == "cleanup log\n"


def test_clean_stop_never_uses_forced_kill() -> None:
    clean_stop = _SERVICE_HELPERS.split("stop_application_cleanly() {", 1)[1].split(
        "\n}\nwait_for_readiness()", 1
    )[0]

    assert "KILL" not in clean_stop
    assert 'kill -INT "$application_pid"' in clean_stop
    assert 'test "$status" -eq 0' in clean_stop
    assert "grep -Fxq 'Stopped server'" in clean_stop
    assert 'test "$attempt" -lt 30' in clean_stop


def test_application_launch_restores_sigint_then_execs_in_same_process(
    tmp_path: Path,
) -> None:
    launch = _SERVICE_HELPERS.split("launch_application() {", 1)[1].split(
        "\n}\napplication_is_non_zombie()", 1
    )[0]

    assert "signal.signal(signal.SIGINT, signal.SIG_DFL)" in launch
    assert "os.execv(sys.argv[1], sys.argv[1:])" in launch
    assert 'application_pid="$!"' in launch
    _service_script(
        tmp_path,
        log_text="Stopped server\n",
        child_status=0,
        command="stop_application_cleanly",
    )
    assert "signal.signal" not in tmp_path.joinpath("service.py").read_text()


def test_reused_comfy_cli_bridge_binds_workspace_child_and_no_local_venv() -> None:
    assert '/opt/venv/bin/python"' in _COMFY_CLI_BRIDGE_PROBE
    assert 'readlink -f "$process/cwd"' in _COMFY_CLI_BRIDGE_PROBE
    assert '"$COMFYUI_PATH"' in _COMFY_CLI_BRIDGE_PROBE
    assert 'test ! -e "$COMFYUI_PATH/.venv"' in _COMFY_CLI_BRIDGE_PROBE
    assert 'test ! -e "$COMFYUI_PATH/venv"' in _COMFY_CLI_BRIDGE_PROBE


def test_common_runtime_proof_namespace_is_complete() -> None:
    python_source = _COMMON_IMAGE_PROBE.split("/opt/venv/bin/python -I - <<'PY'\n", 1)[
        1
    ].split("\nbuild = pathlib.Path", 1)[0]
    namespace: dict[str, object] = {}

    exec(python_source, namespace)

    assert namespace["tomllib"] is tomllib
    assert callable(namespace["canonicalize_name"])
    assert namespace["Version"].__name__ == "Version"
    assert callable(namespace["prove_git_targets"])
    assert callable(namespace["scan_registry_projects_after_git_proof"])
    assert callable(namespace["prove_python_routing"])


def _python_routing_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, object], dict[str, Path]]:
    opt = tmp_path / "opt"
    python_root = opt / "python"
    catalog_key = "cpython-3.13.14-linux-x86_64-gnu"
    catalog_root = python_root / catalog_key
    bin_directory = catalog_root / "bin"
    bin_directory.mkdir(parents=True)
    for directory in (opt, python_root, catalog_root, bin_directory):
        directory.chmod(0o775)
    expected = bin_directory / "python3.13"
    expected.write_text("#!/bin/sh\nexit 0\n")
    expected.chmod(0o775)

    virtual_env = opt / "venv"
    venv_bin = virtual_env / "bin"
    venv_bin.mkdir(parents=True)
    for name in ("python", "python3", "python3.13"):
        venv_bin.joinpath(name).symlink_to(expected)
    for name in ("pip", "pip3"):
        command = venv_bin / name
        command.write_text(f"#!{venv_bin / 'python'}\n")
        command.chmod(0o775)

    plan = {
        "toolchain": {
            "python": {
                "catalog_key": catalog_key,
                "version": "3.13.14",
            }
        }
    }
    namespace: dict[str, object] = {"pathlib": pathlib, "stat": stat}
    exec(_PYTHON_ROUTING_PROOF_SOURCE, namespace)
    arguments: dict[str, object] = {
        "plan": plan,
        "python_root": python_root,
        "virtual_env": virtual_env,
        "owner_uid": os.getuid(),
        "owner_gid": os.getgid(),
        "executable": str(venv_bin / "python"),
        "prefix": str(virtual_env),
        "exec_prefix": str(virtual_env),
        "base_executable": str(expected),
        "python_version": "3.13.14",
    }
    paths = {
        "opt": opt,
        "python_root": python_root,
        "catalog_root": catalog_root,
        "bin_directory": bin_directory,
        "expected": expected,
        "virtual_env": virtual_env,
        "venv_bin": venv_bin,
    }
    return namespace, arguments, paths


def test_python_routing_accepts_uv_venv_symlinks_and_group_writable_owned_tree(
    tmp_path: Path,
) -> None:
    namespace, arguments, paths = _python_routing_fixture(tmp_path)

    observed = namespace["prove_python_routing"](**arguments)

    assert observed == paths["expected"]


@pytest.mark.parametrize(
    "mutation",
    [
        "target-escape",
        "managed-target-second-symlink",
        "managed-parent-symlink",
        "wrong-owner",
        "parent-world-writable",
        "target-world-writable",
        "target-nonexecutable",
        "target-directory",
        "venv-python-regular",
        "alias-wrong-target",
        "alias-regular",
        "pip-symlink",
        "pip-world-writable",
        "pip-nonexecutable",
        "pip-wrong-shebang",
        "wrong-sys-executable",
        "wrong-prefix",
        "wrong-exec-prefix",
        "wrong-base-executable",
        "base-executable-symlink-alias",
        "wrong-python-version",
    ],
)
def test_python_routing_rejects_unsafe_or_mismatched_routes(
    tmp_path: Path, mutation: str
) -> None:
    namespace, arguments, paths = _python_routing_fixture(tmp_path)
    expected = paths["expected"]
    venv_bin = paths["venv_bin"]
    outside = tmp_path / "outside-python"
    outside.write_text("#!/bin/sh\nexit 0\n")
    outside.chmod(0o755)

    if mutation == "target-escape":
        venv_bin.joinpath("python").unlink()
        venv_bin.joinpath("python").symlink_to(outside)
    elif mutation == "managed-target-second-symlink":
        expected.unlink()
        expected.symlink_to(outside)
    elif mutation == "managed-parent-symlink":
        catalog_root = paths["catalog_root"]
        actual_catalog_root = paths["python_root"] / "actual-catalog"
        catalog_root.rename(actual_catalog_root)
        catalog_root.symlink_to(actual_catalog_root, target_is_directory=True)
    elif mutation == "wrong-owner":
        arguments["owner_uid"] = os.getuid() + 1
    elif mutation == "parent-world-writable":
        paths["catalog_root"].chmod(0o777)
    elif mutation == "target-world-writable":
        expected.chmod(0o777)
    elif mutation == "target-nonexecutable":
        expected.chmod(0o664)
    elif mutation == "target-directory":
        expected.unlink()
        expected.mkdir()
    elif mutation == "venv-python-regular":
        python = venv_bin / "python"
        python.unlink()
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
    elif mutation == "alias-wrong-target":
        alias = venv_bin / "python3"
        alias.unlink()
        alias.symlink_to(outside)
    elif mutation == "alias-regular":
        alias = venv_bin / "python3"
        alias.unlink()
        alias.write_text("#!/bin/sh\nexit 0\n")
        alias.chmod(0o755)
    elif mutation == "pip-symlink":
        pip = venv_bin / "pip"
        pip.unlink()
        pip.symlink_to(venv_bin / "pip3")
    elif mutation == "pip-world-writable":
        venv_bin.joinpath("pip").chmod(0o777)
    elif mutation == "pip-nonexecutable":
        venv_bin.joinpath("pip").chmod(0o664)
    elif mutation == "pip-wrong-shebang":
        venv_bin.joinpath("pip").write_text("#!/usr/bin/python\n")
    elif mutation == "wrong-sys-executable":
        arguments["executable"] = str(venv_bin / "python3")
    elif mutation == "wrong-prefix":
        arguments["prefix"] = str(tmp_path)
    elif mutation == "wrong-exec-prefix":
        arguments["exec_prefix"] = str(tmp_path)
    elif mutation == "wrong-base-executable":
        arguments["base_executable"] = str(outside)
    elif mutation == "base-executable-symlink-alias":
        alias = tmp_path / "managed-python-alias"
        alias.symlink_to(expected)
        assert alias.resolve(strict=True) == expected
        arguments["base_executable"] = str(alias)
    else:
        assert mutation == "wrong-python-version"
        arguments["python_version"] = "3.13.13"

    with pytest.raises(AssertionError):
        namespace["prove_python_routing"](**arguments)


def _manager_filesystem_fixture(
    tmp_path: Path,
    *,
    initializer: bool,
) -> tuple[dict[str, object], str, Path, Path, Path, Path, int, int]:
    manager_name = "acceptance_manager"
    site_packages = tmp_path / "site-packages"
    manager_root = site_packages / manager_name
    manager_root.mkdir(parents=True)
    if initializer:
        manager_root.joinpath("__init__.py").write_text("VALUE = 'initialized'\n")
    cm_cli = tmp_path / "bin" / "cm-cli"
    cm_cli.parent.mkdir()
    cm_cli.write_text("#!/opt/venv/bin/python\n")
    cm_cli.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    anchor = site_packages / "comfyui-docker-helper-comfyui.pth"
    anchor.write_text(f"{workspace}\n")
    anchor.chmod(0o444)
    owner_uid = cm_cli.stat().st_uid
    owner_gid = cm_cli.stat().st_gid
    namespace: dict[str, object] = {
        "importlib": importlib,
        "pathlib": pathlib,
        "stat": stat,
        "sys": sys,
    }
    exec(_MANAGER_FILESYSTEM_PROOF_SOURCE, namespace)
    return (
        namespace,
        manager_name,
        site_packages,
        manager_root,
        cm_cli,
        anchor,
        owner_uid,
        owner_gid,
    )


@pytest.mark.parametrize("initializer", [False, True], ids=["namespace", "initialized"])
def test_manager_proof_accepts_supported_package_forms(
    tmp_path: Path,
    initializer: bool,
) -> None:
    (
        namespace,
        manager_name,
        site_packages,
        manager_root,
        cm_cli,
        anchor,
        owner_uid,
        owner_gid,
    ) = _manager_filesystem_fixture(tmp_path, initializer=initializer)
    original_path = sys.path.copy()
    sys.path.insert(0, str(site_packages))
    importlib.invalidate_caches()
    try:
        observed_root, observed_origin = namespace["prove_manager_filesystem"](
            site_packages,
            manager_name,
            cm_cli,
            anchor,
            tmp_path / "workspace",
            owner_uid,
            owner_gid,
        )
        namespace["prove_manager_import"](
            manager_name,
            observed_root,
            observed_origin,
            tmp_path / "workspace",
        )

        imported = sys.modules[manager_name]
        assert observed_root == manager_root
        if initializer:
            assert observed_origin == manager_root / "__init__.py"
            assert imported.__file__ == str(observed_origin)
        else:
            assert observed_origin is None
            assert imported.__file__ is None
    finally:
        sys.modules.pop(manager_name, None)
        sys.path[:] = original_path
        importlib.invalidate_caches()


@pytest.mark.parametrize("mutation", ["same-site-symlink", "no-execute-bit"])
def test_manager_filesystem_proof_rejects_alias_and_nonexecutable_command(
    tmp_path: Path,
    mutation: str,
) -> None:
    (
        namespace,
        manager_name,
        site_packages,
        manager_root,
        cm_cli,
        anchor,
        owner_uid,
        owner_gid,
    ) = _manager_filesystem_fixture(tmp_path, initializer=True)
    if mutation == "same-site-symlink":
        alias = site_packages / "manager-alias"
        manager_root.rename(alias)
        manager_root.symlink_to(alias, target_is_directory=True)
    else:
        cm_cli.chmod(0o644)

    with pytest.raises(AssertionError):
        namespace["prove_manager_filesystem"](
            site_packages,
            manager_name,
            cm_cli,
            anchor,
            tmp_path / "workspace",
            owner_uid,
            owner_gid,
        )


@pytest.mark.parametrize("initializer_kind", ["symlink", "directory", "fifo"])
def test_manager_filesystem_proof_rejects_invalid_initializer(
    tmp_path: Path,
    initializer_kind: str,
) -> None:
    (
        namespace,
        manager_name,
        site_packages,
        manager_root,
        cm_cli,
        anchor,
        owner_uid,
        owner_gid,
    ) = _manager_filesystem_fixture(tmp_path, initializer=False)
    initializer = manager_root / "__init__.py"
    if initializer_kind == "symlink":
        target = tmp_path / "outside.py"
        target.write_text("\n")
        initializer.symlink_to(target)
    elif initializer_kind == "directory":
        initializer.mkdir()
    else:
        os.mkfifo(initializer)

    with pytest.raises(AssertionError):
        namespace["prove_manager_filesystem"](
            site_packages,
            manager_name,
            cm_cli,
            anchor,
            tmp_path / "workspace",
            owner_uid,
            owner_gid,
        )


def test_manager_import_proof_rejects_runtime_path_replacement(tmp_path: Path) -> None:
    (
        namespace,
        manager_name,
        site_packages,
        manager_root,
        cm_cli,
        anchor,
        owner_uid,
        owner_gid,
    ) = _manager_filesystem_fixture(tmp_path, initializer=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    manager_root.joinpath("__init__.py").write_text(f"__path__ = [{str(outside)!r}]\n")
    original_path = sys.path.copy()
    sys.path.insert(0, str(site_packages))
    importlib.invalidate_caches()
    try:
        observed_root, observed_origin = namespace["prove_manager_filesystem"](
            site_packages,
            manager_name,
            cm_cli,
            anchor,
            tmp_path / "workspace",
            owner_uid,
            owner_gid,
        )

        with pytest.raises(AssertionError):
            namespace["prove_manager_import"](
                manager_name,
                observed_root,
                observed_origin,
                tmp_path / "workspace",
            )
    finally:
        sys.modules.pop(manager_name, None)
        sys.path[:] = original_path
        importlib.invalidate_caches()


def _observed_inventory_bytes(order: str) -> bytes:
    def normalize(value: str) -> str:
        return re.sub(r"[-_.]+", "-", value).lower()

    items = sorted(
        (normalize(item.metadata["Name"]), str(Version(item.version)))
        for item in metadata.distributions()
    )
    rows = [f"{name}=={version}" for name, version in items]
    if order == "serialized-row-order":
        rows.sort()
    else:
        assert order == "identity-order"
    return ("\n".join(rows) + "\n").encode()


def _run_inventory_observer(path: Path, order: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _INVENTORY_OBSERVER_SOURCE,
            str(path),
            order,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )


@pytest.mark.parametrize("order", ["identity-order", "serialized-row-order"])
def test_inventory_observer_accepts_producer_specific_canonical_exact_bytes(
    tmp_path: Path, order: str
) -> None:
    inventory = tmp_path / "inventory.txt"
    inventory.write_bytes(_observed_inventory_bytes(order))

    completed = _run_inventory_observer(inventory, order)

    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    assert observed == sorted(observed)


@pytest.mark.parametrize(
    ("content_order", "observer_order"),
    [
        ("identity-order", "serialized-row-order"),
        ("serialized-row-order", "identity-order"),
    ],
)
def test_inventory_observer_rejects_another_producers_order(
    tmp_path: Path, content_order: str, observer_order: str
) -> None:
    identity_order = _observed_inventory_bytes("identity-order")
    serialized_row_order = _observed_inventory_bytes("serialized-row-order")
    assert identity_order != serialized_row_order
    inventory = tmp_path / "inventory.txt"
    inventory.write_bytes(_observed_inventory_bytes(content_order))

    completed = _run_inventory_observer(inventory, observer_order)

    assert completed.returncode != 0


@pytest.mark.parametrize("mutation", ["order", "newline", "duplicate"])
@pytest.mark.parametrize("order", ["identity-order", "serialized-row-order"])
def test_inventory_observer_rejects_noncanonical_bytes(
    tmp_path: Path,
    order: str,
    mutation: str,
) -> None:
    lines = _observed_inventory_bytes(order).splitlines(keepends=True)
    if mutation == "order":
        lines = list(reversed(lines))
    elif mutation == "newline":
        lines[-1] = lines[-1].rstrip(b"\n")
    else:
        lines.append(lines[0])
    inventory = tmp_path / "inventory.txt"
    inventory.write_bytes(b"".join(lines))

    completed = _run_inventory_observer(inventory, order)

    assert completed.returncode != 0


def test_application_fixtures_use_generic_names_and_exact_baseline() -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures" / "comfyui-build" / "configs"
    names = {
        "application-full.toml",
        "application-zero.toml",
        "application-manager-disabled.toml",
        "application-cli-disabled-mixed.toml",
        "application-py314-full.toml",
        "application-py314-zero.toml",
        "application-py314-manager-disabled.toml",
        "application-py314-cli-disabled-mixed.toml",
    }

    dispositions = {
        "application-full.toml": (
            "3.13.14",
            True,
            True,
            ["git", "registry", "git"],
        ),
        "application-zero.toml": ("3.13.14", False, False, []),
        "application-manager-disabled.toml": ("3.13.14", True, False, []),
        "application-cli-disabled-mixed.toml": (
            "3.13.14",
            False,
            True,
            ["git", "registry", "git"],
        ),
        "application-py314-full.toml": (
            "3.14.6",
            True,
            True,
            ["git", "registry", "git"],
        ),
        "application-py314-zero.toml": ("3.14.6", False, False, []),
        "application-py314-manager-disabled.toml": (
            "3.14.6",
            True,
            False,
            [],
        ),
        "application-py314-cli-disabled-mixed.toml": (
            "3.14.6",
            False,
            True,
            ["git", "registry", "git"],
        ),
    }

    for name in names:
        document = tomllib.loads(fixture_root.joinpath(name).read_text())
        comfyui = document["comfyui"]
        python_version, expected_cli, expected_manager, node_types = dispositions[name]
        assert comfyui["version"] == "0.11.0"
        if python_version == "3.14.6":
            assert document["python"] == {
                "version": "3.14.6",
                "uv_version": "0.11.28",
                "uv_tools": [],
            }
        else:
            assert "python" not in document
        assert document["compute_platform"]["cuda"]["version"] == "13.0.3"
        assert document["pytorch"]["version"] == "2.12.1"
        assert comfyui.get("install_cli", True) is expected_cli
        assert comfyui["install_manager"] is expected_manager
        nodes = comfyui.get("custom_nodes", [])
        assert [node["type"] for node in nodes] == node_types
        if nodes:
            assert [
                (node.get("url"), node.get("ref"), node.get("target_dir"))
                for node in nodes
            ] == [
                (
                    "https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git",
                    "609f3afaa74b2f88ef9ce8d939626065e3247469",
                    "git-custom-scripts",
                ),
                (None, None, None),
                (
                    "https://github.com/receyuki/comfyui-prompt-reader-node.git",
                    "a70cbb0c8d1208a01c0eea72e8f2c3668cac3ba7",
                    "comfyui-prompt-reader-node",
                ),
            ]
            assert nodes[0]["pre_install_scripts"] == ["pre.sh", "pre.py"]
            assert nodes[0]["post_install_scripts"] == ["post.sh", "post.py"]
            assert "pre_install_scripts" not in nodes[1]
            assert "post_install_scripts" not in nodes[1]
