"""Tests for safe deterministic Dockerfile rendering."""

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
    RenderPlanValidationError,
    RuntimeHooksPlan,
    build_render_plan,
    with_runtime_hooks_plan,
)
from comfyui_docker_helper.rendering import (
    render_dockerfile,
    serialize_dockerfile_identifier,
    serialize_dockerfile_word,
    serialize_json_array,
    serialize_posix_shell_argument,
)

_NODE_ONLY_LAYER = (
    "RUN --mount=type=bind,source=config.toml,"
    "target=/tmp/cdh/config.toml \\\n"
    "    --mount=type=bind,source=config.lock.toml,"
    "target=/tmp/cdh/config.lock.toml \\\n"
    "    cdh container install-custom-nodes \\\n"
    "      --config /tmp/cdh/config.toml \\\n"
    "      --lock /tmp/cdh/config.lock.toml\n"
)
_FILE_ONLY_LAYER = (
    "RUN --mount=type=bind,source=config.toml,"
    "target=/tmp/cdh/config.toml \\\n"
    "    --mount=type=bind,source=config.lock.toml,"
    "target=/tmp/cdh/config.lock.toml \\\n"
    "    cdh container download-files \\\n"
    "      --config /tmp/cdh/config.toml \\\n"
    "      --lock /tmp/cdh/config.lock.toml\n"
)
_HOOK_LAYER = (
    "RUN --mount=type=bind,source=config.toml,"
    "target=/tmp/cdh/config.toml \\\n"
    "    --mount=type=bind,source=config.lock.toml,"
    "target=/tmp/cdh/config.lock.toml \\\n"
    "    --mount=type=bind,source=scripts,target=/tmp/cdh/scripts \\\n"
    "    cdh container install-custom-nodes \\\n"
    "      --config /tmp/cdh/config.toml \\\n"
    "      --lock /tmp/cdh/config.lock.toml \\\n"
    "      --scripts-dir /tmp/cdh/scripts\n"
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


def assert_fragments_in_order(rendered: str, fragments: list[str]) -> None:
    """Assert each fragment appears after the previous matching fragment."""
    position = 0
    for fragment in fragments:
        next_position = rendered.find(fragment, position)
        assert next_position >= 0, fragment
        position = next_position + len(fragment)


# Dockerfile tests target layer order, bind mounts, copy boundaries, quoting, and
# the runtime entrypoint shape without pinning the whole rendered file.
def test_minimal_dockerfile_has_deterministic_fragments_and_order() -> None:
    """Render the minimal Dockerfile deterministically with required layers."""
    config = make_config()
    plan = build_render_plan(config)

    lockfile = make_lockfile(config)

    first = render_dockerfile(plan, lockfile=lockfile)
    second = render_dockerfile(plan, lockfile=lockfile)

    assert second == first
    assert first.startswith(
        "# syntax=docker/dockerfile:1.7\n\n"
        "ARG UV_IMAGE_TAG=latest\n"
        "ARG CUDA_IMAGE_TAG=12.9.2-cudnn-devel-ubuntu24.04\n"
        "FROM ghcr.io/astral-sh/uv:${UV_IMAGE_TAG} AS uv\n"
        "FROM nvidia/cuda:${CUDA_IMAGE_TAG}\n"
    )
    assert "COPY --from=uv /uv /uvx /bin/" in first
    assert "ARG PYTHON_VERSION=3.12" in first
    assert "ARG PYTORCH_VERSION=2.10" in first
    assert "ARG COMFY_CLI_VERSION=latest" in first
    assert "ARG COMFYUI_VERSION=latest" in first
    assert "ENV VIRTUAL_ENV=/opt/venv" in first
    assert 'ENV PATH="/opt/venv/bin:${PATH}"' in first
    assert "ENV WORKSPACE=/workspace" in first
    assert "ENV COMFYUI_PATH=/workspace/ComfyUI" in first
    assert 'Binary::apt::APT::Keep-Downloaded-Packages "true";' in first
    assert "    openssh-server \\\n" in first
    assert "RUN mkdir -p -- \\\n    /workspace\n" in first
    assert 'uv venv "$VIRTUAL_ENV" --python "${PYTHON_VERSION}" --seed' in first
    assert '"torch==${PYTORCH_VERSION}"' in first
    assert "      -- comfy-cli==1.5.0" in first
    assert 'RUN comfy --skip-prompt --workspace "$COMFYUI_PATH" install' in first
    assert f'test "$comfyui_commit" = {COMMIT_1}' in first
    assert "source=packages/cdh,target=/tmp/cdh/packages/cdh" in first
    assert "COPY runtime/config.toml /opt/cdh/runtime/config.toml" in first
    assert first.endswith(
        'WORKDIR /workspace\nENTRYPOINT ["cdh", "container", "entrypoint"]\n'
    )

    # Preserve the minimal layer sequence while allowing unrelated formatting
    # changes inside individual instructions.
    assert_fragments_in_order(
        first,
        [
            "FROM ghcr.io/astral-sh/uv:${UV_IMAGE_TAG} AS uv",
            "FROM nvidia/cuda:${CUDA_IMAGE_TAG}",
            "COPY --from=uv /uv /uvx /bin/",
            "ARG PYTHON_VERSION=3.12",
            "ENV COMFYUI_PATH=/workspace/ComfyUI",
            "Keep-Downloaded-Packages",
            "apt-get update",
            "RUN mkdir -p",
            "uv python install",
            '"torch==${PYTORCH_VERSION}"',
            "comfy-cli==1.5.0",
            "RUN comfy --skip-prompt",
            "git -C",
            "source=packages/cdh",
            "COPY runtime/config.toml",
            "WORKDIR /workspace",
            'ENTRYPOINT ["cdh", "container", "entrypoint"]',
        ],
    )
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


def test_openssh_server_capability_is_installed_without_default_startup() -> None:
    """Install sshd capability while keeping runtime activation out of Dockerfile."""
    rendered = render_dockerfile(
        build_render_plan(make_config()),
        lockfile=make_lockfile(make_config()),
    )

    assert "    openssh-server \\\n" in rendered
    assert " && rm -f /etc/ssh/ssh_host_* \\\n" in rendered
    assert " && test -x /usr/sbin/sshd\n" in rendered
    assert "policy-rc.d" in rendered
    assert "EXPOSE" not in rendered
    assert "systemctl" not in rendered
    assert "service ssh" not in rendered
    assert "service sshd" not in rendered
    assert "sshd -D" not in rendered
    assert "/usr/sbin/sshd -D" not in rendered
    assert "/usr/sbin/sshd" in rendered
    assert rendered.index("openssh-server") < rendered.index(
        "rm -f /etc/ssh/ssh_host_*"
    )
    assert rendered.index("rm -f /etc/ssh/ssh_host_*") < rendered.index(
        "test -x /usr/sbin/sshd"
    )


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
        "      --index-url https://pypi.org/simple \\\n      -- comfy-cli==1.5.0"
        in rendered
    )
    assert "      --version \\\n      0.26.0 \\" in rendered
    expected_commit_check = (
        'RUN comfyui_commit="$(git -C "$COMFYUI_PATH" rev-parse HEAD)" '
        f'&& test "$comfyui_commit" = {COMMIT_1}'
    )
    assert expected_commit_check in rendered
    assert (
        'uv pip install --python "$VIRTUAL_ENV/bin/python" -- comfy-cli;'
        not in rendered
    )
    assert '--version "$COMFYUI_VERSION"' not in rendered


def test_custom_package_indexes_are_wired_to_their_own_install_layers() -> None:
    """Use each package index for its own install layer."""
    config = make_config()
    config.python.index_url = "https://python.example.com/simple"
    config.python.extra_packages = ["httpx"]
    config.pytorch.index_base_url = "https://torch.example.com/whl"

    rendered = render_dockerfile(
        build_render_plan(config),
        lockfile=make_lockfile(config),
    )

    assert rendered.count("--index-url https://python.example.com/simple") == 4
    assert (
        rendered.count("--index-url https://torch.example.com/whl/${PYTORCH_WHEEL_TAG}")
        == 1
    )
    assert "https://python.example.com/simple/${PYTORCH_WHEEL_TAG}" not in rendered
    assert "https://torch.example.com/whl --" not in rendered
    torch_layer = rendered[
        rendered.index("https://torch.example.com/whl") : rendered.index(
            '"torch==${PYTORCH_VERSION}"'
        )
    ]
    assert "https://python.example.com/simple" not in torch_layer


def test_package_index_urls_are_shell_quoted_in_dockerfile() -> None:
    """Quote index URL values as one shell argument before Docker executes RUN."""
    config = make_config()
    config.python.index_url = "https://python.example.com/simple;touch"
    config.python.extra_packages = ["httpx"]
    config.pytorch.index_base_url = "https://torch.example.com/whl;touch"

    rendered = render_dockerfile(
        build_render_plan(config),
        lockfile=make_lockfile(config),
    )

    assert rendered.count("--index-url 'https://python.example.com/simple;touch'") == 4
    assert (
        "--index-url 'https://torch.example.com/whl;touch'/${PYTORCH_WHEEL_TAG}"
        in rendered
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param(
            "python",
            "https://user:token@example.com/simple",
            id="python-userinfo",
        ),
        pytest.param(
            "pytorch",
            "https://token@example.com/whl",
            id="pytorch-userinfo",
        ),
    ],
)
def test_credential_bearing_index_urls_fail_before_dockerfile_render(
    field: str,
    value: str,
) -> None:
    """Fail closed before credentials can be rendered into Dockerfile source."""
    config = make_config()
    if field == "python":
        config.python.index_url = value
    else:
        config.pytorch.index_base_url = value

    with pytest.raises(RenderPlanValidationError):
        build_render_plan(config)


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
        "      --version \\\n"
        "      nightly \\\n"
        "      --commit \\\n"
        "      1111111111111111111111111111111111111111 \\" in rendered
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

    expected_line = (
        'RUN comfyui_commit="$(git -C "$COMFYUI_PATH" rev-parse HEAD)" '
        f'&& test "$comfyui_commit" = {COMMIT_1}'
    )
    assert lines == [expected_line]
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
                "source=config/",
                "source=scripts",
                "--scripts-dir",
            ),
            id="node-only",
        ),
        pytest.param(
            "file",
            _FILE_ONLY_LAYER.rstrip(),
            (
                "source=config/",
                "source=scripts",
                "--scripts-dir",
            ),
            id="file-only",
        ),
        pytest.param(
            "hook",
            _HOOK_LAYER.rstrip(),
            ("source=config/",),
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
    assert (
        'WORKDIR /workspace\nENTRYPOINT ["cdh", "container", "entrypoint"]' in rendered
    )
    assert "\nCMD " not in rendered
    assert "/workspace/ComfyUI/main.py" not in rendered
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
    config.comfyui.listen = 'value "quoted" $cash \\ path'
    config.comfyui.port = 8190
    config.comfyui.extra_args = ["--preview-method", "auto", "--cpu"]
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

    assert rendered == render_dockerfile(plan, lockfile=make_lockfile(config))

    # User-controlled Dockerfile words and shell arguments must stay quoted at
    # their respective parser boundaries.
    assert 'ENV WORKSPACE="/work dir/\\$cash/\\"quote\\"/back\\\\slash"' in rendered
    assert 'ENV COMFYUI_PATH="/opt/Comfy UI"' in rendered
    assert 'ENV SAFE_VALUE="space \\$cash \\"quote\\" \\\\ backtick` ;"' in rendered
    assert 'ARG PYTHON_VERSION="3.13 rc"' in rendered
    assert "ARG UV_IMAGE_TAG=0.11.23" in rendered
    assert "'--option-like'" not in rendered
    assert "    --option-like \\\n" in rendered
    assert "    'lib'\"'\"'special'" in rendered
    assert (
        "RUN mkdir -p -- \\\n"
        "    '/work dir/$cash/\"quote\"/back\\slash' \\\n"
        "    /opt\n" in rendered
    )
    assert "      --index-url=https://example.invalid \\\n" in rendered
    assert "      'a'\"'\"'b'" in rendered
    assert "      --pre \\\n" in rendered
    assert "      'torchvision==1'" not in rendered
    assert "      torchvision==1" in rendered
    assert "      --skip-manager" not in rendered
    assert rendered.endswith(
        f"WORKDIR {serialize_dockerfile_word(config.system.workspace)}\n"
        'ENTRYPOINT ["cdh", "container", "entrypoint"]\n'
    )
    assert "\nCMD " not in rendered
    assert "/opt/Comfy UI/main.py" not in rendered
    assert plan.comfyui.launch_command[-8:] == (
        "--listen",
        'value "quoted" $cash \\ path',
        "--port",
        "8190",
        "--disable-auto-launch",
        "--preview-method",
        "auto",
        "--cpu",
    )

    # Build inputs stay bind-mounted; only runtime artifacts cross the COPY
    # boundary into the image.
    copy_lines = [line for line in rendered.splitlines() if line.startswith("COPY ")]
    assert copy_lines == [
        "COPY --from=uv /uv /uvx /bin/",
        "COPY runtime/config.toml /opt/cdh/runtime/config.toml",
    ]
    assert rendered.count("source=config.toml,target=/tmp/cdh/config.toml") == 2
    assert (
        rendered.count("source=config.lock.toml,target=/tmp/cdh/config.lock.toml") == 2
    )
    assert rendered.count("source=scripts,target=/tmp/cdh/scripts") == 1
    assert rendered.count("source=packages/cdh,target=/tmp/cdh/packages/cdh") == 1
    assert "COPY packages" not in rendered
    assert "COPY config" not in rendered
    assert "COPY scripts" not in rendered

    assert rendered.count("--index-url https://pypi.org/simple") == 4
    assert rendered.count("--index-url=https://example.invalid") == 1
    assert (
        rendered.count(
            "--index-url https://download.pytorch.org/whl/${PYTORCH_WHEEL_TAG}"
        )
        == 1
    )
    assert rendered.count("--mount=type=cache,target=/root/.cache/uv") == 5

    # Optional feature layers must remain after core installs and before the
    # runtime entrypoint.
    assert_fragments_in_order(
        rendered,
        [
            "FROM nvidia/cuda:${CUDA_IMAGE_TAG}",
            "apt-get install",
            "RUN mkdir -p",
            "uv python install",
            "--index-url https://pypi.org/simple",
            '"torch==${PYTORCH_VERSION}"',
            "--index-url=https://example.invalid",
            "comfy-cli==1.5.0",
            "RUN comfy --skip-prompt",
            "git -C",
            "source=packages/cdh",
            "COPY runtime/config.toml",
            "source=config.toml",
            "source=config.lock.toml",
            "source=scripts",
            "cdh container install-custom-nodes",
            "cdh container download-files",
            "WORKDIR",
            "ENTRYPOINT",
        ],
    )


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
    """Use bind mounts for build inputs and only COPY uv plus runtime defaults."""
    config = make_config()
    rendered = render_dockerfile(
        build_render_plan(config),
        lockfile=make_lockfile(config),
    )

    copy_lines = [line for line in rendered.splitlines() if line.startswith("COPY ")]
    assert copy_lines == [
        "COPY --from=uv /uv /uvx /bin/",
        "COPY runtime/config.toml /opt/cdh/runtime/config.toml",
    ]
    assert "/opt/cdh/runtime/config.toml" in rendered
    assert "COPY packages" not in rendered
    assert "COPY config" not in rendered
    assert "COPY scripts" not in rendered


def test_runtime_hooks_copy_is_conditional() -> None:
    """Bake runtime hook sources only when a host hook source is active."""
    config = make_config()
    plan = with_runtime_hooks_plan(
        build_render_plan(config),
        RuntimeHooksPlan(has_hooks=True, source_dir=Path("/tmp/hooks")),
    )

    rendered = render_dockerfile(plan, lockfile=make_lockfile(config))

    copy_lines = [line for line in rendered.splitlines() if line.startswith("COPY ")]
    assert copy_lines == [
        "COPY --from=uv /uv /uvx /bin/",
        "COPY runtime/config.toml /opt/cdh/runtime/config.toml",
        "COPY runtime/hooks /opt/cdh/runtime/hooks",
    ]
    assert rendered.index("COPY runtime/config.toml") < rendered.index(
        "COPY runtime/hooks"
    )


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
