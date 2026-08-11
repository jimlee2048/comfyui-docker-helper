"""Native Windows evidence for lexical hook inputs and reparse rejection."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfyui_docker_helper.host.identity_providers import (
    FilesystemLocalExecutableIdentityProvider,
)
from comfyui_docker_helper.host.render_service import (
    HostRenderServiceError,
    admit_build_hook_source,
)
from comfyui_docker_helper.host.runtime_hook_inputs import (
    RuntimeHookInputError,
    discover_runtime_hook_inputs,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows paths and junctions",
)
_NATIVE_WINDOWS_COMMAND_TIMEOUT_SECONDS = 30


def test_windows_runtime_hook_discovery_and_identity_use_lexical_unicode_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hooks with spaces 钩子"
    phase = root / "pre-start.d"
    phase.mkdir(parents=True)
    content = b"echo windows\n"
    (phase / "10-start.sh").write_bytes(content)

    inputs = discover_runtime_hook_inputs(
        Path("unused") / ".." / root.name,
        working_directory=tmp_path,
    )
    identity = FilesystemLocalExecutableIdentityProvider().resolve(inputs.requests[0])

    assert inputs.source_root == root
    assert identity.relative_path.as_posix() == "runtime-hooks/pre-start.d/10-start.sh"
    assert identity.digest == f"sha256:{hashlib.sha256(content).hexdigest()}"


def test_windows_runtime_phase_and_build_root_reject_junctions(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "10-start.sh").write_bytes(b"true\n")
    (outside / "build-root").mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    phase_junction = runtime_root / "pre-start.d"
    build_parent_junction = tmp_path / "build hooks"
    _create_junction(phase_junction, outside)
    _create_junction(build_parent_junction, outside)
    build_source = build_parent_junction / "build-root"
    configured = SimpleNamespace(
        config=SimpleNamespace(
            comfyui=SimpleNamespace(
                custom_nodes=(
                    SimpleNamespace(
                        pre_install_hooks=("10-start.sh",),
                        post_install_hooks=(),
                    ),
                )
            )
        )
    )
    try:
        with pytest.raises(RuntimeHookInputError) as runtime_error:
            discover_runtime_hook_inputs(runtime_root, working_directory=None)
        assert runtime_error.value.diagnostics[0].code == "runtime_hooks.symlink"

        with pytest.raises(HostRenderServiceError) as build_error:
            admit_build_hook_source(
                configured,
                build_source,
                tmp_path / "context",
            )
        assert (
            build_error.value.diagnostics[0].code
            == "render.build_hook_source_unavailable"
        )
    finally:
        phase_junction.rmdir()
        build_parent_junction.rmdir()


def _create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=_NATIVE_WINDOWS_COMMAND_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
