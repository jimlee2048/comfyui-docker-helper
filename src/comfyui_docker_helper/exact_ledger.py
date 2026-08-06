"""Single code authority for exact project-owned identities."""

UV_IMAGE_REPOSITORY = "astral/uv"
COMFYUI_REPOSITORY = "https://github.com/comfyanonymous/ComfyUI.git"
# v0.11.0 is the first formal release to declare its runtime requests dependency.
COMFYUI_MINIMUM_VERSION = "0.11.0"
COMFYUI_FLOOR_COMMIT = "09725967cf76304371c390ca1d6483e04061da48"
# v1.7.0 provides the complete isolated-tool/workspace-Python bridge we require.
COMFY_CLI_MINIMUM_VERSION = "1.7.0"
PIP_VERSION = "26.1.2"
DEFAULT_MANAGED_PYTHON_VERSION = "3.13.14"
CUDA_VERSION = "13.0.3"
CUDA_IMAGE_REPOSITORY = "nvidia/cuda"
DEFAULT_CUDA_IMAGE_FLAVOR = "cudnn-devel"
DEFAULT_CUDA_IMAGE_DISTRO = "ubuntu24.04"
CUDA_PROTECTED_REQUIREMENTS = ("torch", "torchaudio", "torchvision")
