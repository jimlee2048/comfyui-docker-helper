"""Run the mandatory final application smoke checks from a narrow payload."""

from __future__ import annotations

import importlib
import json
import pathlib
import sys

_TORCH_IMPORT = "torch-import"
_TORCH_CPU_TENSOR = "torch-cpu-tensor"
_TORCHVISION_IMPORT = "torchvision-import"
_TORCHAUDIO_IMPORT = "torchaudio-import"
_TORCHAUDIO_CPU_RESAMPLE = "torchaudio-cpu-resample"
_FOLDER_PATHS_IMPORT = "comfyui-folder-paths-import"
_COMFY_IMPORT = "comfyui-comfy-import"
_MANAGER_IMPORT = "comfyui-manager-import"
_BASE_CHECKS = (
    _TORCH_IMPORT,
    _TORCH_CPU_TENSOR,
    _FOLDER_PATHS_IMPORT,
    _COMFY_IMPORT,
)
_CHECK_ORDER = (
    _TORCH_IMPORT,
    _TORCH_CPU_TENSOR,
    _TORCHVISION_IMPORT,
    _TORCHAUDIO_IMPORT,
    _TORCHAUDIO_CPU_RESAMPLE,
    _FOLDER_PATHS_IMPORT,
    _COMFY_IMPORT,
    _MANAGER_IMPORT,
)


def _payload() -> tuple[pathlib.Path, tuple[str, ...]]:
    assert len(sys.argv) == 2
    value = json.loads(sys.argv[1])
    assert isinstance(value, dict) and set(value) == {"checks", "workspace"}
    workspace_value = value["workspace"]
    checks_value = value["checks"]
    assert isinstance(workspace_value, str) and workspace_value
    assert isinstance(checks_value, list)
    assert all(isinstance(item, str) for item in checks_value)
    checks = tuple(checks_value)
    assert len(checks) == len(set(checks))
    assert tuple(item for item in _CHECK_ORDER if item in checks) == checks
    assert all(item in checks for item in _BASE_CHECKS)
    assert (_TORCHAUDIO_IMPORT in checks) == (_TORCHAUDIO_CPU_RESAMPLE in checks)
    return pathlib.Path(workspace_value), checks


def _workspace_first(workspace: pathlib.Path) -> None:
    workspace_value = str(workspace)
    sys.path[:] = [
        workspace_value,
        *(entry for entry in sys.path if entry != workspace_value),
    ]


def _torch_import() -> None:
    importlib.import_module("torch")


def _torch_cpu_tensor() -> None:
    torch = importlib.import_module("torch")
    tensor = torch.ones((2,), dtype=torch.float32)
    assert (tensor + 1).sum().item() == 4


def _torchvision_import() -> None:
    importlib.import_module("torchvision")


def _torchaudio_import() -> None:
    importlib.import_module("torchaudio")


def _torchaudio_cpu_resample() -> None:
    torch = importlib.import_module("torch")
    torchaudio = importlib.import_module("torchaudio")
    waveform = torch.ones((1, 1600), dtype=torch.float32)
    resampled = torchaudio.functional.resample(waveform, 16000, 8000)
    assert resampled.shape == (1, 800)


def _folder_paths_import() -> None:
    importlib.import_module("folder_paths")


def _comfy_import() -> None:
    importlib.import_module("comfy")


def _manager_import() -> None:
    importlib.import_module("comfyui_manager")


_CHECKS = {
    _TORCH_IMPORT: _torch_import,
    _TORCH_CPU_TENSOR: _torch_cpu_tensor,
    _TORCHVISION_IMPORT: _torchvision_import,
    _TORCHAUDIO_IMPORT: _torchaudio_import,
    _TORCHAUDIO_CPU_RESAMPLE: _torchaudio_cpu_resample,
    _FOLDER_PATHS_IMPORT: _folder_paths_import,
    _COMFY_IMPORT: _comfy_import,
    _MANAGER_IMPORT: _manager_import,
}


def main() -> None:
    workspace, checks = _payload()
    _workspace_first(workspace)
    for check in checks:
        _CHECKS[check]()
    print(
        json.dumps(
            {"checks": checks, "result": "passed", "stage": "final-build"},
            separators=(",", ":"),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
