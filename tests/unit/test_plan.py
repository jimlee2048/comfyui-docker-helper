"""Tests for immutable deterministic normalized render plans."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from comfyui_docker_helper.config import (
    ArtifactCondition,
    ArtifactKind,
    BuildArgument,
    Config,
    FileConfig,
    GitCustomNodeConfig,
    GitCustomNodePlan,
    Layer,
    OutputArtifact,
    RegistryCustomNodeConfig,
    RegistryCustomNodePlan,
    RenderPlanValidationError,
    build_render_plan,
)


def make_config() -> Config:
    """Return a fresh minimal valid public configuration."""
    return Config.model_validate(
        {
            "compute_platform": {
                "type": "cuda",
                "cuda": {"version": "12.9.2"},
            },
            "pytorch": {"version": "2.10"},
            "comfyui": {"version": "latest"},
        }
    )


def test_minimal_plan_resolves_every_effective_default() -> None:
    """Resolve minimal public config into complete renderer input."""
    plan = build_render_plan(make_config())

    assert plan.base_image == "nvidia/cuda:12.9.2-cudnn-devel-ubuntu24.04"
    assert plan.paths.workspace == "/workspace"
    assert plan.paths.comfyui == "/workspace/ComfyUI"
    assert plan.paths.venv == "/opt/venv"
    assert plan.paths.preinstall_directories == ("/workspace",)
    assert plan.os_packages == (
        "bash",
        "ca-certificates",
        "curl",
        "git",
        "build-essential",
        "aria2",
    )
    assert plan.environment == ()
    assert plan.python.version == "3.12"
    assert plan.python.uv_version == "latest"
    assert plan.python.index_url == "https://pypi.org/simple"
    assert plan.python.extra_packages == ()
    assert plan.pytorch.version == "2.10"
    assert plan.pytorch.wheel_tag == "cu129"
    assert plan.pytorch.index_base_url == "https://download.pytorch.org/whl"
    assert plan.pytorch.requirements == ("torch==2.10",)
    assert plan.comfyui.cli_version == "latest"
    assert plan.comfyui.cli_requirement == "comfy-cli"
    assert plan.comfyui.version == "latest"
    assert plan.comfyui.install_manager is True
    assert plan.comfyui.listen == "0.0.0.0"
    assert plan.comfyui.port == 8188
    assert plan.comfyui.extra_arguments == ()
    assert plan.comfyui.install_arguments == (
        "--nvidia",
        "--version",
        "latest",
        "--skip-torch-or-directml",
        "--fast-deps",
    )
    assert plan.comfyui.launch_arguments == (
        "--listen",
        "0.0.0.0",
        "--port",
        "8188",
        "--disable-auto-launch",
    )
    assert plan.comfyui.launch_command == (
        "python",
        "/workspace/ComfyUI/main.py",
        "--listen",
        "0.0.0.0",
        "--port",
        "8188",
        "--disable-auto-launch",
    )
    assert plan.custom_nodes.items == ()
    assert plan.custom_nodes.update_cache is False
    assert plan.custom_nodes.has_hooks is False
    assert plan.custom_nodes.scripts_source_dir is None
    assert plan.files.items == ()
    assert plan.files.downloader.default == "aria2"
    assert plan.files.downloader.aria2.rpc_port == 6800
    assert plan.files.downloader.aria2.split == 16
    assert plan.files.downloader.aria2.max_connection_per_server == 16
    assert plan.files.downloader.aria2.min_split_size == "1M"
    assert plan.files.downloader.aria2.resume_download is True
    assert plan.files.downloader.httpx.timeout == 60
    assert plan.files.downloader.httpx.retries == 3


def test_minimal_build_arguments_have_exact_names_values_and_order() -> None:
    """Expose only the nine scalar build arguments from spec section 6."""
    plan = build_render_plan(make_config())

    assert plan.build_arguments == (
        BuildArgument("UV_IMAGE_TAG", "latest"),
        BuildArgument("CUDA_IMAGE_TAG", "12.9.2-cudnn-devel-ubuntu24.04"),
        BuildArgument("PYTHON_VERSION", "3.12"),
        BuildArgument("PYTORCH_VERSION", "2.10"),
        BuildArgument("PYTORCH_WHEEL_TAG", "cu129"),
        BuildArgument("COMFY_CLI_VERSION", "latest"),
        BuildArgument("COMFYUI_VERSION", "latest"),
        BuildArgument("UV_LINK_MODE", "copy"),
        BuildArgument("UV_PYTHON_CACHE_DIR", "/root/.cache/uv/python"),
    )


def test_minimal_layers_omit_empty_feature_layers() -> None:
    """Keep required layer order while omitting empty extras, nodes, and files."""
    plan = build_render_plan(make_config())

    assert plan.layers == (
        Layer.BASE_AND_UV,
        Layer.OS_PACKAGES,
        Layer.WORKSPACE_DIRECTORIES,
        Layer.PYTHON_VENV,
        Layer.PYTORCH,
        Layer.COMFY_CLI,
        Layer.COMFYUI,
        Layer.CDH,
        Layer.FINAL,
    )


def test_minimal_manifest_contains_only_always_present_artifacts() -> None:
    """Describe the fixed allowlisted context without conditional directories."""
    manifest = build_render_plan(make_config()).output_manifest

    assert manifest.always == (
        OutputArtifact("Dockerfile", ArtifactKind.FILE),
        OutputArtifact(".cdh-rendered", ArtifactKind.FILE),
        OutputArtifact("runtime/config.toml", ArtifactKind.FILE),
        OutputArtifact("packages/cdh/pyproject.toml", ArtifactKind.FILE),
        OutputArtifact("packages/cdh/src", ArtifactKind.TREE),
    )
    assert manifest.conditional == ()
    assert manifest.all == manifest.always
    assert all(artifact.path != "normalized.toml" for artifact in manifest.all)


@pytest.mark.parametrize(
    ("version", "wheel_tag"),
    [("12.9.2", "cu129"), ("13.0.2", "cu130"), ("12.8", "cu128")],
)
def test_cuda_version_derives_pytorch_wheel_tag(version: str, wheel_tag: str) -> None:
    """Derive the exact CUDA wheel tag from major and minor components."""
    config = make_config()
    config.compute_platform.cuda.version = version

    plan = build_render_plan(config)

    assert plan.pytorch.wheel_tag == wheel_tag
    assert plan.pytorch.index_base_url == "https://download.pytorch.org/whl"


def test_explicit_paths_packages_environment_and_versions_preserve_order() -> None:
    """Resolve explicit scalar values and preserve ordered user collections."""
    config = make_config()
    config.system.workspace = "/srv/work"
    config.system.comfyui_path = "/opt/custom/ComfyUI"
    config.system.extra_packages = ["ffmpeg", "libgl1", "ffmpeg"]
    config.system.env = {"B": "second", "A": "first"}
    config.python.version = "3.13"
    config.python.uv_version = "0.8.0"
    config.python.index_url = "https://mirror.example.com/simple"
    config.python.extra_packages = ["one", "two"]
    config.pytorch.version = "2.11"
    config.pytorch.extra_packages = ["torchvision", "torchaudio"]
    config.comfyui.cli_version = "v2.0RC1"
    config.comfyui.version = "v1.2.3"
    config.comfyui.listen = "127.0.0.1"
    config.comfyui.port = 8190
    config.comfyui.extra_args = ["--preview-method", "auto", "--cpu"]

    plan = build_render_plan(config)

    assert plan.paths.workspace == "/srv/work"
    assert plan.paths.comfyui == "/opt/custom/ComfyUI"
    assert plan.paths.preinstall_directories == ("/srv/work", "/opt/custom")
    assert plan.os_packages[-3:] == ("ffmpeg", "libgl1", "ffmpeg")
    assert [(item.name, item.value) for item in plan.environment] == [
        ("B", "second"),
        ("A", "first"),
    ]
    assert plan.python.version == "3.13"
    assert plan.python.uv_version == "0.8.0"
    assert plan.python.index_url == "https://mirror.example.com/simple"
    assert plan.python.extra_packages == ("one", "two")
    assert plan.pytorch.version == "2.11"
    assert plan.pytorch.requirements == (
        "torch==2.11",
        "torchvision",
        "torchaudio",
    )
    assert plan.comfyui.cli_version == "2.0rc1"
    assert plan.comfyui.cli_requirement == "comfy-cli==2.0rc1"
    assert plan.comfyui.version == "1.2.3"
    assert plan.comfyui.listen == "127.0.0.1"
    assert plan.comfyui.port == 8190
    assert plan.comfyui.extra_arguments == ("--preview-method", "auto", "--cpu")
    assert plan.comfyui.launch_arguments == (
        "--listen",
        "127.0.0.1",
        "--port",
        "8190",
        "--disable-auto-launch",
        "--preview-method",
        "auto",
        "--cpu",
    )
    assert plan.comfyui.launch_command == (
        "python",
        "/opt/custom/ComfyUI/main.py",
        "--listen",
        "127.0.0.1",
        "--port",
        "8190",
        "--disable-auto-launch",
        "--preview-method",
        "auto",
        "--cpu",
    )
    assert plan.layers[5] is Layer.PYTHON_EXTRAS


def test_manager_disabled_adds_only_skip_manager_install_flag() -> None:
    """Represent the Manager-off ComfyUI install decision explicitly."""
    config = make_config()
    config.comfyui.install_manager = False

    plan = build_render_plan(config)

    assert plan.comfyui.install_manager is False
    assert plan.comfyui.install_arguments[-1] == "--skip-manager"


def test_custom_node_targets_order_cache_and_omitted_versions(
    tmp_path: Path,
) -> None:
    """Resolve ordered node targets without inventing omitted version or ref values."""
    (tmp_path / "before.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "after.py").write_text("pass\n", encoding="utf-8")
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "plain-registry",
                "pre_install_scripts": ["before.sh"],
            }
        ),
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/plain.git"}
        ),
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "versioned", "version": "latest"}
        ),
        GitCustomNodeConfig.model_validate(
            {
                "type": "git",
                "url": "https://example.com/ref.git",
                "ref": "v1.2.3",
                "target_dir": "explicit-ref",
                "post_install_scripts": ["after.py"],
            }
        ),
    ]

    plan = build_render_plan(config, scripts_dir=tmp_path)

    assert [item.target for item in plan.custom_nodes.items] == [
        "plain-registry",
        "https://example.com/plain.git",
        "versioned@latest",
        "https://example.com/ref.git@v1.2.3",
    ]
    first, second, third, fourth = plan.custom_nodes.items
    assert isinstance(first, RegistryCustomNodePlan)
    assert first.version is None
    assert first.pre_install_scripts == ("before.sh",)
    assert isinstance(second, GitCustomNodePlan)
    assert second.ref is None
    assert second.target_dir is None
    assert isinstance(third, RegistryCustomNodePlan)
    assert third.version == "latest"
    assert isinstance(fourth, GitCustomNodePlan)
    assert fourth.ref == "v1.2.3"
    assert fourth.target_dir == "explicit-ref"
    assert fourth.post_install_scripts == ("after.py",)
    assert plan.custom_nodes.update_cache is True
    assert plan.custom_nodes.has_hooks is True
    assert plan.custom_nodes.scripts_source_dir == tmp_path.resolve()


def test_git_only_nodes_do_not_request_registry_cache_update() -> None:
    """Omit the registry cache operation for an all-Git node plan."""
    config = make_config()
    config.comfyui.custom_nodes = [
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/node.git"}
        )
    ]

    plan = build_render_plan(config)

    assert plan.custom_nodes.update_cache is False
    assert plan.custom_nodes.has_hooks is False


def test_duplicate_git_target_dirs_are_refused_before_plan_construction() -> None:
    """Surface effective Git clone directory collisions as validation diagnostics."""
    config = make_config()
    config.comfyui.custom_nodes = [
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/node.git"}
        ),
        GitCustomNodeConfig.model_validate(
            {
                "type": "git",
                "url": "https://example.org/other.git",
                "target_dir": "node",
            }
        ),
    ]

    with pytest.raises(RenderPlanValidationError) as raised:
        build_render_plan(config)

    assert [
        (diagnostic.path, diagnostic.code) for diagnostic in raised.value.diagnostics
    ] == [
        (
            ("comfyui", "custom_nodes", 1, "target_dir"),
            "custom_node.duplicate_git_target_dir",
        )
    ]


def test_files_resolve_defaults_targets_order_and_downloader_settings() -> None:
    """Materialize every dependent file default and both backend settings."""
    config = make_config()
    config.system.workspace = "/srv"
    config.cdh.default_downloader = "httpx"
    config.cdh.downloader.aria2.rpc_port = 7000
    config.cdh.downloader.aria2.split = 8
    config.cdh.downloader.aria2.max_connection_per_server = 4
    config.cdh.downloader.aria2.min_split_size = "2M"
    config.cdh.downloader.aria2.resume_download = False
    config.cdh.downloader.httpx.timeout = 90.5
    config.cdh.downloader.httpx.retries = 5
    config.files = [
        FileConfig(
            url="https://example.com/first.bin",
            dir="models/checkpoints",
            filename="renamed.bin",
            overwrite=True,
            downloader="aria2",
        ),
        FileConfig(
            url="https://example.com/path/second.safetensors?download=1",
            dir="models/loras",
            filename="second.safetensors",
        ),
    ]

    plan = build_render_plan(config)

    assert plan.paths.comfyui == "/srv/ComfyUI"
    assert [item.url for item in plan.files.items] == [
        "https://example.com/first.bin",
        "https://example.com/path/second.safetensors?download=1",
    ]
    first, second = plan.files.items
    assert first.filename == "renamed.bin"
    assert first.target == "/srv/ComfyUI/models/checkpoints/renamed.bin"
    assert first.overwrite is True
    assert first.downloader == "aria2"
    assert second.filename == "second.safetensors"
    assert second.target == "/srv/ComfyUI/models/loras/second.safetensors"
    assert second.overwrite is False
    assert second.downloader == "httpx"
    assert plan.files.downloader.default == "httpx"
    assert plan.files.downloader.aria2.rpc_port == 7000
    assert plan.files.downloader.aria2.split == 8
    assert plan.files.downloader.aria2.max_connection_per_server == 4
    assert plan.files.downloader.aria2.min_split_size == "2M"
    assert plan.files.downloader.aria2.resume_download is False
    assert plan.files.downloader.httpx.timeout == 90.5
    assert plan.files.downloader.httpx.retries == 5


def test_full_feature_layers_and_manifest_are_conditional_and_ordered(
    tmp_path: Path,
) -> None:
    """Activate extras, nodes, files, root artifacts, and scripts in spec order."""
    (tmp_path / "hook.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    config = make_config()
    config.python.extra_packages = ["xformers"]
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "node",
                "pre_install_scripts": ["hook.sh"],
            }
        )
    ]
    config.files = [
        FileConfig(url="https://example.com/file", dir="models", filename="file")
    ]

    plan = build_render_plan(config, scripts_dir=tmp_path)

    assert plan.layers == (
        Layer.BASE_AND_UV,
        Layer.OS_PACKAGES,
        Layer.WORKSPACE_DIRECTORIES,
        Layer.PYTHON_VENV,
        Layer.PYTORCH,
        Layer.PYTHON_EXTRAS,
        Layer.COMFY_CLI,
        Layer.COMFYUI,
        Layer.CDH,
        Layer.CUSTOM_NODES,
        Layer.FILES,
        Layer.FINAL,
    )
    assert plan.output_manifest.conditional == (
        OutputArtifact(
            "scripts",
            ArtifactKind.TREE,
            ArtifactCondition.HOOKS,
        ),
    )


def test_nodes_without_hooks_omit_scripts_artifact() -> None:
    """Do not emit hook scripts when hooks are absent."""
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate({"type": "registry", "id": "node"})
    ]

    manifest = build_render_plan(config).output_manifest

    assert manifest.conditional == ()


def test_plan_construction_is_deterministic_and_detached_from_public_mutation() -> None:
    """Build equal plans and freeze ordered collections independently of config."""
    config = make_config()
    config.system.extra_packages = ["ffmpeg"]
    config.python.extra_packages = ["xformers"]

    first = build_render_plan(config)
    second = build_render_plan(config)
    config.system.extra_packages.append("later")
    config.python.extra_packages.append("later")

    assert first == second
    assert first.os_packages[-1] == "ffmpeg"
    assert first.python.extra_packages == ("xformers",)
    with pytest.raises(FrozenInstanceError):
        first.base_image = "changed"  # type: ignore[misc]


def test_invalid_config_is_refused_with_original_diagnostics() -> None:
    """Never construct a partial unsafe plan from business-invalid input."""
    config = make_config()
    config.compute_platform.type = "rocm"
    config.system.workspace = "relative"

    with pytest.raises(RenderPlanValidationError) as raised:
        build_render_plan(config)

    assert [
        (diagnostic.path, diagnostic.code) for diagnostic in raised.value.diagnostics
    ] == [
        (("compute_platform", "type"), "compute_platform.unsupported_backend"),
        (("system", "workspace"), "system.path_not_absolute"),
    ]


def test_hooks_without_scripts_dir_are_refused_before_plan_construction() -> None:
    """Preserve the conditional hook source validation guard in the builder."""
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "node",
                "pre_install_scripts": ["hook.sh"],
            }
        )
    ]

    with pytest.raises(RenderPlanValidationError) as raised:
        build_render_plan(config)

    assert [diagnostic.code for diagnostic in raised.value.diagnostics] == [
        "hook.scripts_dir_required"
    ]
