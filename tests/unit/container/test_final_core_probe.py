"""Final-only application probe behavior and payload contracts."""

from __future__ import annotations

import json
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from comfyui_docker_helper.container import final_manifest

_PROBE = final_manifest._FINAL_CORE_PROBE_PATH
_BASE_CHECKS = (
    "torch-import",
    "torch-cpu-tensor",
    "comfyui-folder-paths-import",
    "comfyui-comfy-import",
)
_FULL_CHECKS = (
    "torch-import",
    "torch-cpu-tensor",
    "torchvision-import",
    "torchaudio-import",
    "torchaudio-cpu-resample",
    "comfyui-folder-paths-import",
    "comfyui-comfy-import",
    "comfyui-manager-import",
)


def _environment(tmp_path: Path, *, full: bool) -> tuple[Path, Path]:
    virtual_env = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(virtual_env)
    site_packages = (
        virtual_env
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    torch = site_packages / "torch"
    torch.mkdir()
    torch.joinpath("__init__.py").write_text(
        "float32 = object()\n"
        "class _Scalar:\n"
        "    def __init__(self, value): self.value = value\n"
        "    def item(self): return self.value\n"
        "class Tensor:\n"
        "    def __init__(self, shape, value=1):\n"
        "        self.shape, self.value = shape, value\n"
        "    def __add__(self, value): return Tensor(self.shape, self.value + value)\n"
        "    def sum(self):\n"
        "        count = 1\n"
        "        for dimension in self.shape: count *= dimension\n"
        "        return _Scalar(count * self.value)\n"
        "def ones(shape, dtype=None): return Tensor(shape)\n"
    )
    if full:
        torchvision = site_packages / "torchvision"
        torchvision.mkdir()
        torchvision.joinpath("__init__.py").write_text("")
        torchaudio = site_packages / "torchaudio"
        torchaudio.mkdir()
        torchaudio.joinpath("__init__.py").write_text("from . import functional\n")
        torchaudio.joinpath("functional.py").write_text(
            "from torch import Tensor\n"
            "def resample(waveform, source_rate, target_rate):\n"
            "    assert source_rate == 16000 and target_rate == 8000\n"
            "    return Tensor((waveform.shape[0], waveform.shape[1] // 2))\n"
        )
        manager = site_packages / "comfyui_manager"
        manager.mkdir()
        manager.joinpath("__init__.py").write_text("")

    workspace = tmp_path / "ComfyUI"
    workspace.mkdir()
    workspace_value = str(workspace)
    workspace.joinpath("folder_paths.py").write_text(
        f"import sys\nassert sys.path[0] == {workspace_value!r}\n"
    )
    comfy = workspace / "comfy"
    comfy.mkdir()
    comfy.joinpath("__init__.py").write_text(
        f"import sys\nassert sys.path[0] == {workspace_value!r}\n"
    )
    return virtual_env / "bin/python", workspace


def _run(
    python: Path,
    workspace: Path,
    checks: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    payload = json.dumps(
        {"checks": checks, "workspace": str(workspace)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return subprocess.run(
        (python, "-I", _PROBE, payload),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# The real resource executes the complete selected check set in one subprocess.
def test_final_core_probe_reports_only_successfully_executed_full_checks(
    tmp_path: Path,
) -> None:
    python, workspace = _environment(tmp_path, full=True)

    completed = _run(python, workspace, _FULL_CHECKS)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "checks": list(_FULL_CHECKS),
        "result": "passed",
        "stage": "final-build",
    }


# Check selection excludes optional imports, rejects malformed sets, and
# propagates real capability failures.
def test_final_core_probe_omits_unselected_optional_imports(tmp_path: Path) -> None:
    python, workspace = _environment(tmp_path, full=False)

    completed = _run(python, workspace, _BASE_CHECKS)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["checks"] == list(_BASE_CHECKS)


@pytest.mark.parametrize(
    "checks",
    [
        (*reversed(_BASE_CHECKS),),
        ("torch-import", "torch-cpu-tensor"),
        (*_BASE_CHECKS, "torchaudio-import"),
    ],
)
def test_final_core_probe_rejects_malformed_check_sets(
    tmp_path: Path,
    checks: tuple[str, ...],
) -> None:
    python, workspace = _environment(tmp_path, full=True)

    assert _run(python, workspace, checks).returncode != 0


def test_final_core_probe_propagates_capability_failure(tmp_path: Path) -> None:
    python, workspace = _environment(tmp_path, full=False)
    python_minor = f"python{sys.version_info.major}.{sys.version_info.minor}"
    torch = workspace.parent / "venv" / "lib" / python_minor / "site-packages/torch"
    torch.joinpath("__init__.py").write_text("raise RuntimeError('broken torch')\n")

    assert _run(python, workspace, _BASE_CHECKS).returncode != 0
