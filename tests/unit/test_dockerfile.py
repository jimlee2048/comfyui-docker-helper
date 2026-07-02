"""Tests for safe deterministic Dockerfile rendering."""

# ruff: noqa: E501 -- Exact Dockerfile snapshots contain required long instructions.

import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from importlib import resources
from pathlib import Path

import pytest
from tests.artifact_helpers import COMMIT_1, make_lockfile

from comfyui_docker_helper.config import (
    BuildArgument,
    Config,
    FileConfig,
    LockedComfyUI,
    Lockfile,
    LockManifest,
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
    uv pip install --python "$VIRTUAL_ENV/bin/python" -- comfy-cli==1.5.0

RUN comfy --skip-prompt --workspace "$COMFYUI_PATH" install \\
      --nvidia \\
      --version \\
      0.26.0 \\
      --skip-torch-or-directml \\
      --fast-deps
RUN comfyui_commit="$(git -C "$COMFYUI_PATH" rev-parse HEAD)" && test "$comfyui_commit" = 1111111111111111111111111111111111111111

RUN --mount=type=bind,source=packages/cdh,target=/tmp/cdh/packages/cdh \\
    --mount=type=cache,target=/root/.cache/uv \\
    uv pip install --python "$VIRTUAL_ENV/bin/python" -- /tmp/cdh/packages/cdh

WORKDIR /workspace
CMD ["python", "/workspace/ComfyUI/main.py", "--listen", "0.0.0.0", "--disable-auto-launch"]
"""

_NODE_ONLY_LAYER = r"""RUN --mount=type=bind,source=config.toml,target=/tmp/cdh/config.toml \
    --mount=type=bind,source=config.lock.toml,target=/tmp/cdh/config.lock.toml \
    cdh container install-custom-nodes \
      --config /tmp/cdh/config.toml \
      --lock /tmp/cdh/config.lock.toml
"""
_FILE_ONLY_LAYER = r"""RUN --mount=type=bind,source=config.toml,target=/tmp/cdh/config.toml \
    --mount=type=bind,source=config.lock.toml,target=/tmp/cdh/config.lock.toml \
    cdh container download-files \
      --config /tmp/cdh/config.toml \
      --lock /tmp/cdh/config.lock.toml
"""
_HOOK_LAYER = r"""RUN --mount=type=bind,source=config.toml,target=/tmp/cdh/config.toml \
    --mount=type=bind,source=config.lock.toml,target=/tmp/cdh/config.lock.toml \
    --mount=type=bind,source=scripts,target=/tmp/cdh/scripts \
    cdh container install-custom-nodes \
      --config /tmp/cdh/config.toml \
      --lock /tmp/cdh/config.lock.toml \
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
    uv pip install --python "$VIRTUAL_ENV/bin/python" -- comfy-cli==1.5.0

RUN comfy --skip-prompt --workspace "$COMFYUI_PATH" install \
      --nvidia \
      --version \
      0.26.0 \
      --skip-torch-or-directml \
      --fast-deps
RUN comfyui_commit="$(git -C "$COMFYUI_PATH" rev-parse HEAD)" && test "$comfyui_commit" = 1111111111111111111111111111111111111111

RUN --mount=type=bind,source=packages/cdh,target=/tmp/cdh/packages/cdh \
    --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python "$VIRTUAL_ENV/bin/python" -- /tmp/cdh/packages/cdh

RUN --mount=type=bind,source=config.toml,target=/tmp/cdh/config.toml \
    --mount=type=bind,source=config.lock.toml,target=/tmp/cdh/config.lock.toml \
    --mount=type=bind,source=scripts,target=/tmp/cdh/scripts \
    cdh container install-custom-nodes \
      --config /tmp/cdh/config.toml \
      --lock /tmp/cdh/config.lock.toml \
      --scripts-dir /tmp/cdh/scripts
RUN --mount=type=bind,source=config.toml,target=/tmp/cdh/config.toml \
    --mount=type=bind,source=config.lock.toml,target=/tmp/cdh/config.lock.toml \
    cdh container download-files \
      --config /tmp/cdh/config.toml \
      --lock /tmp/cdh/config.lock.toml

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
    config = make_config()
    plan = build_render_plan(config)

    lockfile = make_lockfile(config)

    first = render_dockerfile(plan, lockfile=lockfile)
    second = render_dockerfile(plan, lockfile=lockfile)

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


def test_stable_comfyui_and_cli_replay_use_locked_versions() -> None:
    """Replay stable ComfyUI and comfy-cli from config.lock.toml."""
    config = make_config()
    config.comfyui.version = "latest"
    config.comfyui.cli_version = "latest"

    rendered = render_dockerfile(
        build_render_plan(config),
        lockfile=make_lockfile(config),
    )

    assert (
        'uv pip install --python "$VIRTUAL_ENV/bin/python" -- comfy-cli==1.5.0'
        in rendered
    )
    assert "      --version \\\n      0.26.0 \\" in rendered
    assert (
        f'RUN comfyui_commit="$(git -C "$COMFYUI_PATH" rev-parse HEAD)" && test "$comfyui_commit" = {COMMIT_1}'
        in rendered
    )
    assert (
        'uv pip install --python "$VIRTUAL_ENV/bin/python" -- comfy-cli;'
        not in rendered
    )
    assert '--version "$COMFYUI_VERSION"' not in rendered


def test_stable_comfyui_commit_verification_command_succeeds_for_match(
    tmp_path: Path,
) -> None:
    """Execute the rendered stable commit verification command against fake git."""
    command = _rendered_stable_commit_verification_shell_command()
    result = _run_stable_commit_verification_command(
        command,
        tmp_path=tmp_path,
        git_output=COMMIT_1,
        git_exit_code=0,
    )

    assert result.returncode == 0


@pytest.mark.parametrize(
    ("git_output", "git_exit_code"),
    [
        pytest.param("2" * 40, 0, id="mismatched-output"),
        pytest.param(COMMIT_1, 1, id="git-failure"),
    ],
)
def test_stable_comfyui_commit_verification_command_fails_when_unverified(
    tmp_path: Path,
    git_output: str,
    git_exit_code: int,
) -> None:
    """Treat mismatches and git failures as unable to verify the stable commit."""
    command = _rendered_stable_commit_verification_shell_command()
    result = _run_stable_commit_verification_command(
        command,
        tmp_path=tmp_path,
        git_output=git_output,
        git_exit_code=git_exit_code,
    )

    assert result.returncode != 0


def test_nightly_comfyui_replay_uses_locked_commit_without_stable_verify() -> None:
    """Replay nightly as version nightly plus the locked commit."""
    config = make_config()
    config.comfyui.version = "nightly"
    lockfile = make_lockfile(config).model_copy(
        update={
            "comfyui": LockedComfyUI(
                repo=make_lockfile(config).comfyui.repo,
                commit=COMMIT_1,
                version=None,
                cli_version="1.5.0",
            )
        }
    )

    rendered = render_dockerfile(
        build_render_plan(config),
        lockfile=lockfile,
    )

    assert (
        "      --version \\\n      nightly \\\n      --commit \\\n      1111111111111111111111111111111111111111 \\"
        in rendered
    )
    assert "rev-parse HEAD" not in rendered


def _rendered_stable_commit_verification_shell_command() -> str:
    rendered = render_dockerfile(
        build_render_plan(make_config()),
        lockfile=make_lockfile(make_config()),
    )
    lines = [
        line
        for line in rendered.splitlines()
        if 'git -C "$COMFYUI_PATH" rev-parse HEAD' in line
    ]

    assert lines == [
        f'RUN comfyui_commit="$(git -C "$COMFYUI_PATH" rev-parse HEAD)" && test "$comfyui_commit" = {COMMIT_1}'
    ]
    return lines[0].removeprefix("RUN ")


def _run_stable_commit_verification_command(
    command: str,
    *,
    tmp_path: Path,
    git_output: str,
    git_exit_code: int,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'test "$#" -eq 4 || exit 64\n'
        'test "$1" = -C || exit 64\n'
        'test "$3" = rev-parse || exit 64\n'
        'test "$4" = HEAD || exit 64\n'
        'printf "%s\\n" "$CDH_FAKE_GIT_OUTPUT"\n'
        'exit "$CDH_FAKE_GIT_EXIT_CODE"\n'
    )
    fake_git.chmod(0o755)
    comfyui_path = tmp_path / "ComfyUI"
    comfyui_path.mkdir()

    return subprocess.run(
        command,
        shell=True,
        executable="/bin/sh",
        cwd=tmp_path,
        env={
            "PATH": str(fake_bin),
            "COMFYUI_PATH": str(comfyui_path),
            "CDH_FAKE_GIT_OUTPUT": git_output,
            "CDH_FAKE_GIT_EXIT_CODE": str(git_exit_code),
        },
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("lockfile", "message"),
    [
        pytest.param(None, "requires config.lock.toml selections", id="missing-lock"),
        pytest.param(
            Lockfile(
                schema_version=1,
                manifest=LockManifest(
                    lock_input_digest="sha256:" + "0" * 64,
                    git_custom_nodes_input_digest="sha256:" + "1" * 64,
                ),
                comfyui=LockedComfyUI(
                    repo="https://github.com/comfyanonymous/ComfyUI.git",
                    version="0.26.0",
                    commit=COMMIT_1,
                    cli_version="",
                ),
            ),
            r"missing required \[comfyui\].cli_version",
            id="missing-cli-version",
        ),
        pytest.param(
            Lockfile(
                schema_version=1,
                manifest=LockManifest(
                    lock_input_digest="sha256:" + "0" * 64,
                    git_custom_nodes_input_digest="sha256:" + "1" * 64,
                ),
                comfyui=LockedComfyUI(
                    repo="https://github.com/comfyanonymous/ComfyUI.git",
                    version="",
                    commit=COMMIT_1,
                    cli_version="1.5.0",
                ),
            ),
            r"missing required \[comfyui\].version",
            id="missing-stable-version",
        ),
        pytest.param(
            Lockfile(
                schema_version=1,
                manifest=LockManifest(
                    lock_input_digest="sha256:" + "0" * 64,
                    git_custom_nodes_input_digest="sha256:" + "1" * 64,
                ),
                comfyui=LockedComfyUI(
                    repo="https://github.com/comfyanonymous/ComfyUI.git",
                    version=None,
                    commit="",
                    cli_version="1.5.0",
                ),
            ),
            r"missing required \[comfyui\].commit",
            id="missing-nightly-commit",
        ),
    ],
)
def test_renderer_rejects_missing_locked_comfyui_fields(
    lockfile: Lockfile | None,
    message: str,
) -> None:
    """Fail closed before rendering install commands without lock selections."""
    with pytest.raises(ValueError, match=message):
        render_dockerfile(build_render_plan(make_config()), lockfile=lockfile)


@pytest.mark.parametrize(
    ("feature", "expected_fragment", "absent_fragments"),
    [
        pytest.param(
            "node",
            _NODE_ONLY_LAYER.rstrip(),
            (
                "source=config/custom-nodes.toml",
                "source=config/files.toml",
                "source=scripts",
                "--scripts-dir",
            ),
            id="node-only",
        ),
        pytest.param(
            "file",
            _FILE_ONLY_LAYER.rstrip(),
            (
                "source=config/custom-nodes.toml",
                "source=config/files.toml",
                "source=scripts",
                "--scripts-dir",
            ),
            id="file-only",
        ),
        pytest.param(
            "hook",
            _HOOK_LAYER.rstrip(),
            ("source=config/custom-nodes.toml", "source=config/files.toml"),
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

    rendered = render_dockerfile(
        build_render_plan(config, scripts_dir=scripts_dir),
        lockfile=make_lockfile(config),
    )

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
        assert rendered.index("source=config.lock.toml") < rendered.index(
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
    rendered = render_dockerfile(plan, lockfile=make_lockfile(config))

    assert rendered == FULL_DOCKERFILE
    assert rendered == render_dockerfile(plan, lockfile=make_lockfile(config))
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
        "comfy-cli==1.5.0",
        "RUN comfy --skip-prompt",
        "git -C",
        "source=packages/cdh",
        "source=config.toml",
        "source=config.lock.toml",
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

    rendered = render_dockerfile(
        build_render_plan(config),
        lockfile=make_lockfile(config),
    )

    assert rendered.count("--skip-manager") == 1
    assert rendered.index("--fast-deps") < rendered.index("--skip-manager")


def test_build_only_inputs_are_never_persistently_copied() -> None:
    """Use bind mounts for every generated input and COPY only official uv binaries."""
    config = make_config()
    rendered = render_dockerfile(
        build_render_plan(config),
        lockfile=make_lockfile(config),
    )

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
        render_dockerfile(invalid, lockfile=make_lockfile(make_config()))


def test_packaged_template_is_available_through_import_resources() -> None:
    """Include the Jinja template as installed package data."""
    template = resources.files("comfyui_docker_helper").joinpath(
        "templates", "Dockerfile.j2"
    )

    assert template.is_file()
    assert template.read_text(encoding="utf-8").startswith(
        "# syntax=docker/dockerfile:1.7\n"
    )
