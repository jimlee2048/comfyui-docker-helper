"""Tests for public configuration models and TOML loading."""

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config import (
    Aria2Config,
    BuildConfig,
    CdhConfig,
    CdhDownloaderConfig,
    ComfyUIConfig,
    ComputePlatformConfig,
    Config,
    CudaConfig,
    FileConfig,
    GitCustomNodeConfig,
    GitLockedCustomNode,
    HttpxConfig,
    LockedComfyUI,
    Lockfile,
    LockManifest,
    PythonConfig,
    PyTorchConfig,
    RegistryCustomNodeConfig,
    RegistryLockedCustomNode,
    SystemConfig,
    load_config,
)

MINIMAL_CONFIG = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"

[comfyui]
version = "latest"
"""


COMPLETE_CONFIG = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"
image_flavor = "cudnn-devel"
image_distro = "ubuntu24.04"

[system]
workspace = "/workspace"
comfyui_path = "/workspace/ComfyUI"
extra_packages = ["ffmpeg", "libgl1"]

[system.env]
HF_HOME = "/workspace/.cache/huggingface"

[python]
version = "3.12"
uv_version = "latest"
index_url = "https://mirror.example.com/pypi/simple"
extra_packages = ["xformers"]

[pytorch]
version = "2.10"
index_base_url = "https://mirror.example.com/pytorch/whl"
extra_packages = ["torchvision", "torchaudio"]

[cdh]
default_downloader = "aria2"
default_download_mode = "sync"

[cdh.downloader.aria2]
rpc_port = 6800
split = 16
max_connection_per_server = 16
min_split_size = "1M"
resume_download = true

[cdh.downloader.httpx]
timeout = 60.5
retries = 3

[build]
tags = ["my-comfy:dev", "registry.example.com/team/my-comfy:dev"]
output = "push"

[comfyui]
version = "latest"
cli_version = "latest"
install_manager = true
launch_args = ["--listen", "0.0.0.0", "--disable-auto-launch"]

[[comfyui.custom_nodes]]
type = "registry"
id = "comfyui-impact-pack"
version = "latest"
pre_install_scripts = ["registry-pre.sh"]
post_install_scripts = ["registry-post.py"]

[[comfyui.custom_nodes]]
type = "git"
url = "https://github.com/example/ComfyUI-Example.git"
ref = "v1.2.3"
target_dir = "ComfyUI-Example"
pre_install_scripts = ["git-pre.py"]
post_install_scripts = ["git-post.sh"]

[[files]]
url = "https://example.com/first.safetensors"
dir = "models/checkpoints"
filename = "first.safetensors"
overwrite = false
downloader = "aria2"

[[files]]
url = "https://example.com/second.safetensors"
dir = "models/loras"
filename = "second.safetensors"
"""


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    """Return a helper that writes one TOML document for a loader test."""

    def write(document: str) -> Path:
        path = tmp_path / "config.toml"
        path.write_text(document, encoding="utf-8")
        return path

    return write


def test_minimal_config_expands_static_defaults(
    write_config: Callable[[str], Path],
) -> None:
    """Require core blocks and expand complete defaults inside optional fields."""
    config = load_config(write_config(MINIMAL_CONFIG))

    assert config.compute_platform.type == "cuda"
    assert config.cdh.model_dump() == {
        "default_downloader": "aria2",
        "default_download_mode": "sync",
        "downloader": {
            "aria2": {
                "rpc_port": 6800,
                "split": 16,
                "max_connection_per_server": 16,
                "min_split_size": "1M",
                "resume_download": True,
            },
            "httpx": {"timeout": 60, "retries": 3},
        },
    }
    assert config.compute_platform.cuda.model_dump() == {
        "version": "12.9.2",
        "image_flavor": "cudnn-devel",
        "image_distro": "ubuntu24.04",
    }
    assert config.system.model_dump() == {
        "workspace": "/workspace",
        "comfyui_path": None,
        "extra_packages": [],
        "env": {},
    }
    assert config.python.model_dump() == {
        "version": "3.12",
        "uv_version": "latest",
        "index_url": "https://pypi.org/simple",
        "extra_packages": [],
    }
    assert config.pytorch.model_dump() == {
        "version": "2.10",
        "index_base_url": "https://download.pytorch.org/whl",
        "extra_packages": [],
    }
    assert config.build.model_dump() == {"tags": [], "output": "load"}
    assert config.comfyui.model_dump() == {
        "version": "latest",
        "cli_version": "latest",
        "install_manager": True,
        "launch_args": ["--listen", "0.0.0.0", "--disable-auto-launch"],
        "custom_nodes": [],
    }
    assert config.files == []


def test_complete_config_covers_every_top_level_block(
    write_config: Callable[[str], Path],
) -> None:
    """Load explicit values for every block and both custom-node variants."""
    config = load_config(write_config(COMPLETE_CONFIG))

    assert config.system.extra_packages == ["ffmpeg", "libgl1"]
    assert config.system.env == {"HF_HOME": "/workspace/.cache/huggingface"}
    assert config.cdh.downloader.aria2.split == 16
    assert config.cdh.downloader.httpx.timeout == 60.5
    assert config.build.tags == [
        "my-comfy:dev",
        "registry.example.com/team/my-comfy:dev",
    ]
    assert config.build.output == "push"
    assert config.python.index_url == "https://mirror.example.com/pypi/simple"
    assert config.pytorch.index_base_url == "https://mirror.example.com/pytorch/whl"
    assert config.python.extra_packages == ["xformers"]
    assert config.pytorch.extra_packages == ["torchvision", "torchaudio"]
    assert isinstance(config.comfyui.custom_nodes[0], RegistryCustomNodeConfig)
    assert isinstance(config.comfyui.custom_nodes[1], GitCustomNodeConfig)
    assert config.comfyui.custom_nodes[1].target_dir == "ComfyUI-Example"
    assert config.files[0].filename == "first.safetensors"
    assert config.files[1].filename == "second.safetensors"
    assert config.files[1].downloader is None


def test_semantic_lists_preserve_input_order(
    write_config: Callable[[str], Path],
) -> None:
    """Preserve package, launch, node, hook, and file declaration order."""
    config = load_config(write_config(COMPLETE_CONFIG))

    assert config.system.extra_packages == ["ffmpeg", "libgl1"]
    assert config.python.extra_packages == ["xformers"]
    assert config.pytorch.extra_packages == ["torchvision", "torchaudio"]
    assert config.comfyui.launch_args == [
        "--listen",
        "0.0.0.0",
        "--disable-auto-launch",
    ]
    assert [node.type for node in config.comfyui.custom_nodes] == [
        "registry",
        "git",
    ]
    assert config.comfyui.custom_nodes[0].pre_install_scripts == ["registry-pre.sh"]
    assert [file.url for file in config.files] == [
        "https://example.com/first.safetensors",
        "https://example.com/second.safetensors",
    ]


def test_default_factories_do_not_share_mutable_state(
    write_config: Callable[[str], Path],
) -> None:
    """Give each parsed document independent list, mapping, and nested defaults."""
    first = load_config(write_config(MINIMAL_CONFIG))
    second = load_config(write_config(MINIMAL_CONFIG))

    first.system.extra_packages.append("ffmpeg")
    first.system.env["A"] = "B"
    first.cdh.downloader.aria2.split = 8
    first.comfyui.launch_args.append("--cpu")

    assert second.system.extra_packages == []
    assert second.system.env == {}
    assert second.cdh.downloader.aria2.split == 16
    assert second.comfyui.launch_args == [
        "--listen",
        "0.0.0.0",
        "--disable-auto-launch",
    ]


def test_item_models_expand_only_static_defaults(
    write_config: Callable[[str], Path],
) -> None:
    """Default item hooks and flags while retaining dependent values as unset."""
    document = (
        MINIMAL_CONFIG
        + """
[[comfyui.custom_nodes]]
type = "registry"
id = "registry-node"

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/git-node.git"

[[files]]
url = "https://example.com/model.safetensors"
dir = "models"
filename = "model.safetensors"
"""
    )

    config = load_config(write_config(document))

    registry, git = config.comfyui.custom_nodes
    assert registry.version is None
    assert registry.pre_install_scripts == []
    assert registry.post_install_scripts == []
    assert git.ref is None
    assert git.target_dir is None
    assert git.pre_install_scripts == []
    assert git.post_install_scripts == []
    assert config.files[0].filename == "model.safetensors"
    assert config.files[0].overwrite is False
    assert config.files[0].downloader is None


def test_load_config_merges_partial_files_before_structural_validation(
    tmp_path: Path,
) -> None:
    """Allow layered overrides that are only valid after raw TOML merge."""
    base = tmp_path / "base.toml"
    profile = tmp_path / "profile.toml"
    local = tmp_path / "local.toml"
    base.write_text(
        MINIMAL_CONFIG
        + """
[system]
workspace = "/workspace"
extra_packages = ["base"]

[system.env]
PROFILE = "base"

[[files]]
url = "https://example.com/base.bin"
dir = "models"
filename = "model.bin"
""",
        encoding="utf-8",
    )
    profile.write_text(
        """
[system]
extra_packages = ["profile"]

[system.env]
PROFILE = "profile"
EXTRA = "yes"

[[files]]
url = "https://example.com/profile.bin"
dir = "models"
filename = "model.bin"
overwrite = true
""",
        encoding="utf-8",
    )
    local.write_text(
        """
[cdh]
default_downloader = "httpx"
""",
        encoding="utf-8",
    )

    config = load_config([base, profile, local])

    assert config.system.workspace == "/workspace"
    assert config.system.extra_packages == ["profile"]
    assert config.system.env == {"PROFILE": "profile", "EXTRA": "yes"}
    assert config.files[0].url == "https://example.com/profile.bin"
    assert config.files[0].overwrite is True
    assert config.cdh.default_downloader == "httpx"


@pytest.mark.parametrize(
    "model",
    [
        Config,
        BuildConfig,
        CdhConfig,
        CdhDownloaderConfig,
        ComputePlatformConfig,
        CudaConfig,
        SystemConfig,
        PythonConfig,
        PyTorchConfig,
        Aria2Config,
        HttpxConfig,
        ComfyUIConfig,
        Lockfile,
        LockManifest,
        LockedComfyUI,
        RegistryLockedCustomNode,
        GitLockedCustomNode,
        RegistryCustomNodeConfig,
        GitCustomNodeConfig,
        FileConfig,
    ],
)
def test_every_public_model_inherits_strict_default_validating_policy(model) -> None:
    """Apply the same typo, coercion, and default policy at every model boundary."""
    assert model.model_config["extra"] == "forbid"
    assert model.model_config["strict"] is True
    assert model.model_config["validate_default"] is True


@pytest.mark.parametrize(
    "document",
    [
        "unknown = true\n" + MINIMAL_CONFIG,
        MINIMAL_CONFIG.replace(
            'version = "12.9.2"',
            'version = "12.9.2"\nunknown = true',
        ),
        MINIMAL_CONFIG.replace(
            '[pytorch]\nversion = "2.10"',
            '[pytorch]\nversion = "2.10"\nunknown = true',
        ),
    ],
)
def test_unknown_fields_are_rejected_at_any_depth(
    write_config: Callable[[str], Path], document: str
) -> None:
    """Forbid typo-like fields in root and nested blocks."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_config(write_config(document))


def test_old_top_level_downloader_is_an_ordinary_unknown_section(
    write_config: Callable[[str], Path],
) -> None:
    """Reject the removed downloader table through the generic extra path."""
    document = (
        MINIMAL_CONFIG
        + """
[downloader]
default = "aria2"
"""
    )

    with pytest.raises(ValidationError) as raised:
        load_config(write_config(document))

    assert {(error["loc"], error["type"]) for error in raised.value.errors()} == {
        (("downloader",), "extra_forbidden")
    }


@pytest.mark.parametrize(
    ("document", "error_type"),
    [
        ('[pytorch]\nversion = "2.10"\n', "missing"),
        (
            '[compute_platform]\ntype = "cuda"\n[pytorch]\nversion = "2.10"\n',
            "missing",
        ),
        (
            MINIMAL_CONFIG.replace('\n[comfyui]\nversion = "latest"\n', ""),
            "missing",
        ),
        (
            MINIMAL_CONFIG.replace('version = "latest"', 'cli_version = "latest"'),
            "missing",
        ),
        (MINIMAL_CONFIG.replace('version = "2.10"', "extra_packages = []"), "missing"),
        (MINIMAL_CONFIG.replace('type = "cuda"\n', ""), "missing"),
        (MINIMAL_CONFIG.replace('version = "12.9.2"\n', ""), "missing"),
        (MINIMAL_CONFIG.replace('type = "cuda"', "type = 7"), "string_type"),
        (
            MINIMAL_CONFIG.replace(
                'version = "2.10"',
                'version = "2.10"\nextra_packages = [1]',
            ),
            "string_type",
        ),
        (
            MINIMAL_CONFIG
            + '\n[[files]]\nurl = "https://example.com/a"\ndir = "models"\n',
            "missing",
        ),
    ],
)
def test_required_and_declared_type_errors(
    write_config: Callable[[str], Path], document: str, error_type: str
) -> None:
    """Report missing required blocks/fields and invalid declared types."""
    with pytest.raises(ValidationError) as raised:
        load_config(write_config(document))

    assert error_type in {error["type"] for error in raised.value.errors()}


@pytest.mark.parametrize(
    "replacement",
    [
        "rpc_port = 6800.0",
        'rpc_port = "6800"',
        "resume_download = 1",
        'timeout = "60"',
        "retries = 3.0",
        "install_manager = 1",
        'overwrite = "false"',
    ],
)
def test_strict_models_do_not_coerce_values(
    write_config: Callable[[str], Path], replacement: str
) -> None:
    """Reject coercible values for declared integer, number, and bool fields."""
    sections = {
        "rpc_port": "\n[cdh.downloader.aria2]\n",
        "resume_download": "\n[cdh.downloader.aria2]\n",
        "timeout": "\n[cdh.downloader.httpx]\n",
        "retries": "\n[cdh.downloader.httpx]\n",
        "install_manager": "\n[comfyui]\n",
        "overwrite": (
            '\n[[files]]\nurl = "https://example.com/a"\n'
            'dir = "models"\nfilename = "a.bin"\n'
        ),
    }
    field = replacement.split(" =", maxsplit=1)[0]
    if field == "install_manager":
        document = MINIMAL_CONFIG.replace(
            'version = "latest"',
            'version = "latest"\ninstall_manager = 1',
        )
    else:
        document = MINIMAL_CONFIG + sections[field] + replacement + "\n"

    with pytest.raises(ValidationError):
        load_config(write_config(document))


@pytest.mark.parametrize(
    "node",
    [
        'type = "archive"\nid = "node"',
        'type = "registry"\nurl = "https://example.com/node.git"',
        'type = "git"\nid = "node"',
        'id = "node"',
    ],
)
def test_discriminated_custom_node_union_errors(
    write_config: Callable[[str], Path], node: str
) -> None:
    """Require a recognized discriminator and variant-specific required fields."""
    document = MINIMAL_CONFIG + "\n[[comfyui.custom_nodes]]\n" + node + "\n"

    with pytest.raises(ValidationError):
        load_config(write_config(document))


@pytest.mark.parametrize(
    ("node", "expected_location"),
    [
        (
            (
                'type = "registry"\n'
                'id = "registry-node"\n'
                'url = "https://example.com/node.git"'
            ),
            ("comfyui", "custom_nodes", 0, "registry", "url"),
        ),
        (
            ('type = "git"\nurl = "https://example.com/node.git"\nversion = "1.0"'),
            ("comfyui", "custom_nodes", 0, "git", "version"),
        ),
        (
            ('type = "registry"\nid = "registry-node"\ntarget_dir = "node"'),
            ("comfyui", "custom_nodes", 0, "registry", "target_dir"),
        ),
    ],
)
def test_custom_node_variants_reject_cross_variant_fields(
    write_config: Callable[[str], Path],
    node: str,
    expected_location: tuple[str | int, ...],
) -> None:
    """Forbid fields owned by the other node variant after discrimination."""
    document = MINIMAL_CONFIG + "\n[[comfyui.custom_nodes]]\n" + node + "\n"

    with pytest.raises(ValidationError) as raised:
        load_config(write_config(document))

    assert {(error["loc"], error["type"]) for error in raised.value.errors()} == {
        (expected_location, "extra_forbidden")
    }


def test_one_document_collects_independent_structural_errors(
    write_config: Callable[[str], Path],
) -> None:
    """Collect stable missing, type, and extra errors from separate branches."""
    document = """\
unknown_root = true

[compute_platform]
type = 7

[compute_platform.cuda]
unexpected = true

[pytorch]
extra_packages = [1]
"""

    with pytest.raises(ValidationError) as raised:
        load_config(write_config(document))

    assert {(error["loc"], error["type"]) for error in raised.value.errors()} == {
        (("compute_platform", "type"), "string_type"),
        (("compute_platform", "cuda", "version"), "missing"),
        (("compute_platform", "cuda", "unexpected"), "extra_forbidden"),
        (("pytorch", "version"), "missing"),
        (("pytorch", "extra_packages", 0), "string_type"),
        (("comfyui",), "missing"),
        (("unknown_root",), "extra_forbidden"),
    }
