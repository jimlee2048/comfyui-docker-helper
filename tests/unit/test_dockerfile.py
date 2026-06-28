"""Tests for safe deterministic Dockerfile rendering."""

# ruff: noqa: E501 -- Exact Dockerfile snapshots contain required long instructions.

import json
from collections.abc import Callable
from dataclasses import replace
from importlib import resources
from pathlib import Path

import pytest

from comfyui_docker_helper.config import (
    BuildArgument,
    Config,
    FileConfig,
    RegistryCustomNodeConfig,
    build_render_plan,
)
from comfyui_docker_helper.rendering import (
    render_dockerfile,
    serialize_dockerfile_identifier,
    serialize_dockerfile_word,
    serialize_json_array,
    serialize_posix_shell_argument,
)

MINIMAL_DOCKERFILE = """# syntax=docker/dockerfile:1.7

ARG UV_IMAGE_TAG=latest
ARG CUDA_IMAGE_TAG=12.9.2-cudnn-devel-ubuntu24.04
FROM ghcr.io/astral-sh/uv:${UV_IMAGE_TAG} AS uv
FROM nvidia/cuda:${CUDA_IMAGE_TAG}

COPY --from=uv /uv /uvx /bin/

ARG PYTHON_VERSION=3.12
ARG PYTORCH_VERSION=2.10
ARG PYTORCH_WHEEL_TAG=cu129
ARG COMFY_CLI_VERSION=latest
ARG COMFYUI_VERSION=latest
ARG UV_LINK_MODE=copy
ARG UV_PYTHON_CACHE_DIR=/root/.cache/uv/python

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
ENV WORKSPACE=/workspace
ENV COMFYUI_PATH=/workspace/ComfyUI

# Keep APT downloads so BuildKit cache mounts can retain and reuse them.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \\
 && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \\
    --mount=type=cache,target=/var/lib/apt,sharing=locked \\
    apt-get update \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends -- \\
    bash \\
    ca-certificates \\
    curl \\
    git \\
    build-essential \\
    aria2
RUN mkdir -p -- \\
    /workspace

RUN --mount=type=cache,target=/root/.cache/uv \\
    uv python install -- "${PYTHON_VERSION}" \\
 && uv venv "$VIRTUAL_ENV" --python "${PYTHON_VERSION}" --seed

RUN --mount=type=cache,target=/root/.cache/uv \\
    uv pip install --python "$VIRTUAL_ENV/bin/python" \\
      --index-url "https://download.pytorch.org/whl/${PYTORCH_WHEEL_TAG}" \\
      -- \\
      "torch==${PYTORCH_VERSION}"
RUN --mount=type=cache,target=/root/.cache/uv \\
    if [ "$COMFY_CLI_VERSION" = latest ]; then \\
      uv pip install --python "$VIRTUAL_ENV/bin/python" -- comfy-cli; \\
    else \\
      uv pip install --python "$VIRTUAL_ENV/bin/python" -- "comfy-cli==${COMFY_CLI_VERSION}"; \\
    fi

RUN comfy --skip-prompt --workspace "$COMFYUI_PATH" install \\
      --nvidia \\
      --version "$COMFYUI_VERSION" \\
      --skip-torch-or-directml \\
      --fast-deps
RUN --mount=type=bind,source=packages/cdh,target=/tmp/cdh/packages/cdh \\
    --mount=type=cache,target=/root/.cache/uv \\
    uv pip install --python "$VIRTUAL_ENV/bin/python" -- /tmp/cdh/packages/cdh

WORKDIR /workspace
CMD ["python", "/workspace/ComfyUI/main.py", "--listen", "0.0.0.0", "--disable-auto-launch"]
"""

_NODE_ONLY_LAYER = r"""RUN --mount=type=bind,source=config/custom-nodes.toml,target=/tmp/cdh/config/custom-nodes.toml \
    cdh container install-custom-nodes \
      --config /tmp/cdh/config/custom-nodes.toml
"""
_FILE_ONLY_LAYER = r"""RUN --mount=type=bind,source=config/files.toml,target=/tmp/cdh/config/files.toml \
    cdh container download-files \
      --config /tmp/cdh/config/files.toml
"""
_HOOK_LAYER = r"""RUN --mount=type=bind,source=config/custom-nodes.toml,target=/tmp/cdh/config/custom-nodes.toml \
    --mount=type=bind,source=scripts,target=/tmp/cdh/scripts \
    cdh container install-custom-nodes \
      --config /tmp/cdh/config/custom-nodes.toml \
      --scripts-dir /tmp/cdh/scripts
"""

FULL_DOCKERFILE = r"""# syntax=docker/dockerfile:1.7

ARG UV_IMAGE_TAG=0.11.23
ARG CUDA_IMAGE_TAG=12.9.2-cudnn-devel-ubuntu24.04
FROM ghcr.io/astral-sh/uv:${UV_IMAGE_TAG} AS uv
FROM nvidia/cuda:${CUDA_IMAGE_TAG}

COPY --from=uv /uv /uvx /bin/

ARG PYTHON_VERSION="3.13 rc"
ARG PYTORCH_VERSION=2.11
ARG PYTORCH_WHEEL_TAG=cu129
ARG COMFY_CLI_VERSION=2.0
ARG COMFYUI_VERSION=1.2.3
ARG UV_LINK_MODE=copy
ARG UV_PYTHON_CACHE_DIR=/root/.cache/uv/python

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
ENV WORKSPACE="/work dir/\$cash/\"quote\"/back\\slash"
ENV COMFYUI_PATH="/opt/Comfy UI"
ENV SAFE_VALUE="space \$cash \"quote\" \\ backtick` ;"

# Keep APT downloads so BuildKit cache mounts can retain and reuse them.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
 && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends -- \
    bash \
    ca-certificates \
    curl \
    git \
    build-essential \
    aria2 \
    --option-like \
    'lib'"'"'special'
RUN mkdir -p -- \
    '/work dir/$cash/"quote"/back\slash' \
    /opt

RUN --mount=type=cache,target=/root/.cache/uv \
    uv python install -- "${PYTHON_VERSION}" \
 && uv venv "$VIRTUAL_ENV" --python "${PYTHON_VERSION}" --seed

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python "$VIRTUAL_ENV/bin/python" \
      --index-url "https://download.pytorch.org/whl/${PYTORCH_WHEEL_TAG}" \
      -- \
      "torch==${PYTORCH_VERSION}" \
      --pre \
      torchvision==1
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python "$VIRTUAL_ENV/bin/python" -- \
      --index-url=https://example.invalid \
      'a'"'"'b'
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$COMFY_CLI_VERSION" = latest ]; then \
      uv pip install --python "$VIRTUAL_ENV/bin/python" -- comfy-cli; \
    else \
      uv pip install --python "$VIRTUAL_ENV/bin/python" -- "comfy-cli==${COMFY_CLI_VERSION}"; \
    fi

RUN comfy --skip-prompt --workspace "$COMFYUI_PATH" install \
      --nvidia \
      --version "$COMFYUI_VERSION" \
      --skip-torch-or-directml \
      --fast-deps
RUN --mount=type=bind,source=packages/cdh,target=/tmp/cdh/packages/cdh \
    --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python "$VIRTUAL_ENV/bin/python" -- /tmp/cdh/packages/cdh

RUN --mount=type=bind,source=config/custom-nodes.toml,target=/tmp/cdh/config/custom-nodes.toml \
    --mount=type=bind,source=scripts,target=/tmp/cdh/scripts \
    cdh container install-custom-nodes \
      --config /tmp/cdh/config/custom-nodes.toml \
      --scripts-dir /tmp/cdh/scripts
RUN --mount=type=bind,source=config/files.toml,target=/tmp/cdh/config/files.toml \
    cdh container download-files \
      --config /tmp/cdh/config/files.toml

WORKDIR "/work dir/\$cash/\"quote\"/back\\slash"
CMD ["python", "/opt/Comfy UI/main.py", "--listen", "value \"quoted\" $cash \\ path"]
"""


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


def test_minimal_dockerfile_matches_complete_deterministic_snapshot() -> None:
    """Render every fixed minimal instruction exactly once and in spec order."""
    plan = build_render_plan(make_config())

    first = render_dockerfile(plan)
    second = render_dockerfile(plan)

    assert first == MINIMAL_DOCKERFILE
    assert second == first
    assert first.count("--mount=type=cache,target=/root/.cache/uv") == 4
    assert "WORKSPACE" not in {item.name for item in plan.build_arguments}
    assert "COMFYUI_PATH" not in {item.name for item in plan.build_arguments}
    assert "ENV UV_LINK_MODE" not in first
    assert "ENV UV_PYTHON_CACHE_DIR" not in first
    assert first.index("ARG UV_LINK_MODE=copy") < first.index("uv python install")
    assert first.index("ARG UV_PYTHON_CACHE_DIR=/root/.cache/uv/python") < (
        first.index("uv python install")
    )
    assert "rm -rf /var/lib/apt/lists" not in first
    assert "readonly" not in first
    assert "normalized.toml" not in first


@pytest.mark.parametrize(
    ("feature", "expected_fragment", "absent_fragments"),
    [
        pytest.param(
            "node",
            _NODE_ONLY_LAYER.rstrip(),
            ("source=config/files.toml", "source=scripts", "--scripts-dir"),
            id="node-only",
        ),
        pytest.param(
            "file",
            _FILE_ONLY_LAYER.rstrip(),
            ("source=config/custom-nodes.toml", "source=scripts", "--scripts-dir"),
            id="file-only",
        ),
        pytest.param(
            "hook",
            _HOOK_LAYER.rstrip(),
            ("source=config/files.toml",),
            id="hook-only",
        ),
    ],
)
def test_optional_helper_layers_are_emitted_only_for_enabled_features(
    feature: str,
    expected_fragment: str,
    absent_fragments: tuple[str, ...],
    tmp_path: Path,
) -> None:
    """Emit only the helper layer and bind mounts required by enabled features."""
    config = make_config()
    scripts_dir = None
    if feature == "node":
        config.comfyui.custom_nodes = [
            RegistryCustomNodeConfig.model_validate({"type": "registry", "id": "node"})
        ]
    elif feature == "file":
        config.files = [
            FileConfig(
                url="https://example.com/model.bin",
                dir="models",
                filename="model.bin",
            )
        ]
    elif feature == "hook":
        (tmp_path / "before.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        scripts_dir = tmp_path
        config.comfyui.custom_nodes = [
            RegistryCustomNodeConfig.model_validate(
                {
                    "type": "registry",
                    "id": "node",
                    "pre_install_scripts": ["before.sh"],
                }
            )
        ]

    rendered = render_dockerfile(build_render_plan(config, scripts_dir=scripts_dir))

    assert expected_fragment in rendered
    assert rendered.count("cdh container install-custom-nodes") == (
        0 if feature == "file" else 1
    )
    assert rendered.count("cdh container download-files") == (
        1 if feature == "file" else 0
    )
    assert "WORKDIR /workspace\nCMD " in rendered
    for fragment in absent_fragments:
        assert fragment not in rendered
    if feature == "hook":
        assert rendered.count("source=scripts,target=/tmp/cdh/scripts") == 1
        assert rendered.count("--scripts-dir /tmp/cdh/scripts") == 1
        assert rendered.index("source=config/custom-nodes.toml") < rendered.index(
            "source=scripts"
        )


def test_full_dockerfile_quotes_user_values_and_preserves_layer_order(
    tmp_path: Path,
) -> None:
    """Render all optional features safely and in the required linear order."""
    (tmp_path / "before.py").write_text("pass\n", encoding="utf-8")
    config = make_config()
    config.system.workspace = '/work dir/$cash/"quote"/back\\slash'
    config.system.comfyui_path = "/opt/Comfy UI"
    config.system.extra_packages = ["--option-like", "lib'special"]
    config.system.env = {"SAFE_VALUE": 'space $cash "quote" \\ backtick` ;'}
    config.python.version = "3.13 rc"
    config.python.uv_version = "0.11.23"
    config.python.extra_packages = ["--index-url=https://example.invalid", "a'b"]
    config.pytorch.version = "2.11"
    config.pytorch.extra_packages = ["--pre", "torchvision==1"]
    config.comfyui.cli_version = "2.0"
    config.comfyui.version = "v1.2.3"
    config.comfyui.install_manager = True
    config.comfyui.launch_args = ["--listen", 'value "quoted" $cash \\ path']
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "node",
                "pre_install_scripts": ["before.py"],
            }
        )
    ]
    config.files = [
        FileConfig(url="https://example.com/a.bin", dir="models", filename="a.bin")
    ]

    plan = build_render_plan(config, scripts_dir=tmp_path)
    rendered = render_dockerfile(plan)

    assert rendered == FULL_DOCKERFILE
    assert rendered == render_dockerfile(plan)
    assert 'ENV WORKSPACE="/work dir/\\$cash/\\"quote\\"/back\\\\slash"' in rendered
    assert 'ENV SAFE_VALUE="space \\$cash \\"quote\\" \\\\ backtick` ;"' in rendered
    assert "'--option-like'" not in rendered
    assert "    --option-like \\\n" in rendered
    assert "    'lib'\"'\"'special'" in rendered
    assert "      --index-url=https://example.invalid \\\n" in rendered
    assert "      'a'\"'\"'b'" in rendered
    assert "      --pre \\\n" in rendered
    assert "      'torchvision==1'" not in rendered
    assert "      torchvision==1" in rendered
    assert "      --skip-manager" not in rendered
    assert rendered.endswith(
        f"WORKDIR {serialize_dockerfile_word(config.system.workspace)}\n"
        f"CMD {json.dumps(list(plan.comfyui.launch_command), ensure_ascii=False)}\n"
    )

    ordered_fragments = [
        "FROM nvidia/cuda:${CUDA_IMAGE_TAG}",
        "apt-get install",
        "RUN mkdir -p",
        "uv python install",
        '"torch==${PYTORCH_VERSION}"',
        "--index-url=https://example.invalid",
        'if [ "$COMFY_CLI_VERSION" = latest ]',
        "RUN comfy --skip-prompt",
        "source=packages/cdh",
        "source=config/custom-nodes.toml",
        "source=config/files.toml",
        "WORKDIR",
        "CMD",
    ]
    positions = [rendered.index(fragment) for fragment in ordered_fragments]
    assert positions == sorted(positions)
    assert rendered.count("--mount=type=cache,target=/root/.cache/uv") == 5


def test_manager_off_adds_skip_manager_to_comfy_install() -> None:
    """Emit the approved Manager-off flag only in the ComfyUI layer."""
    config = make_config()
    config.comfyui.install_manager = False

    rendered = render_dockerfile(build_render_plan(config))

    assert rendered.count("--skip-manager") == 1
    assert rendered.index("--fast-deps") < rendered.index("--skip-manager")


def test_build_only_inputs_are_never_persistently_copied() -> None:
    """Use bind mounts for every generated input and COPY only official uv binaries."""
    rendered = render_dockerfile(build_render_plan(make_config()))

    copy_lines = [line for line in rendered.splitlines() if line.startswith("COPY ")]
    assert copy_lines == ["COPY --from=uv /uv /uvx /bin/"]
    assert "/opt/cdh/" not in rendered
    assert "COPY packages" not in rendered
    assert "COPY config" not in rendered
    assert "COPY scripts" not in rendered


def test_serializers_handle_adversarial_literal_values() -> None:
    """Keep Dockerfile, shell, and JSON parsing boundaries distinct."""
    value = 'space "quote" $cash \\ backtick` ; #'

    assert serialize_dockerfile_word(value) == (
        '"space \\"quote\\" \\$cash \\\\ backtick` ; #"'
    )
    assert serialize_posix_shell_argument(value) == (
        "'space \"quote\" $cash \\ backtick` ; #'"
    )
    assert serialize_json_array([value]) == json.dumps([value], ensure_ascii=False)
    assert serialize_dockerfile_identifier("SAFE_123") == "SAFE_123"


@pytest.mark.parametrize("character", ["\0", "\r", "\n"])
@pytest.mark.parametrize(
    "serializer",
    [
        serialize_dockerfile_word,
        serialize_posix_shell_argument,
        lambda value: serialize_json_array([value]),
    ],
)
def test_serializers_defensively_reject_source_line_controls(
    serializer: Callable[[str], str],
    character: str,
) -> None:
    """Reject unsafe plans even if callers bypass public config validation."""
    with pytest.raises(ValueError, match="must not contain"):
        serializer(f"before{character}after")


@pytest.mark.parametrize("name", ["", "1BAD", "HAS-DASH", "HAS SPACE"])
def test_identifier_serializer_rejects_invalid_environment_names(name: str) -> None:
    """Defend the template boundary independently of config validation."""
    with pytest.raises(ValueError, match="must match"):
        serialize_dockerfile_identifier(name)


def test_renderer_rejects_noncanonical_build_argument_contract() -> None:
    """Refuse an ad-hoc plan that could bypass required template arguments."""
    plan = build_render_plan(make_config())
    invalid = replace(
        plan,
        build_arguments=(*plan.build_arguments, BuildArgument("EXTRA", "value")),
    )

    with pytest.raises(ValueError, match="build arguments"):
        render_dockerfile(invalid)


def test_packaged_template_is_available_through_import_resources() -> None:
    """Include the Jinja template as installed package data."""
    template = resources.files("comfyui_docker_helper").joinpath(
        "templates", "Dockerfile.j2"
    )

    assert template.is_file()
    assert template.read_text(encoding="utf-8").startswith(
        "# syntax=docker/dockerfile:1.7\n"
    )
