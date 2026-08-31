"""Renderer, materializer, and phase-admission contracts."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import tomllib
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError
from tests.build_plan_support import (
    CANONICAL_WORKSPACE_PROFILE_CONTENT,
    accepted_resolution,
    build_plan,
    canonical_wheel,
    final_config,
)

from comfyui_docker_helper.build_ssh import KNOWN_HOSTS_MOUNTS
from comfyui_docker_helper.config.authored.models import FinalConfig
from comfyui_docker_helper.config.authored.validation.structure import (
    validate_final_config_structure,
)
from comfyui_docker_helper.config.planning.build_plan import (
    BuildPlan,
    DownloaderCredentialRoutePlan,
    GitCredentialRoutePlan,
    HookPlan,
    LocalFilePlan,
    LocalTreeMemberPlan,
    LocalTreePlan,
    build_plan_digest,
    dump_build_plan_json,
    parse_build_plan_json,
)
from comfyui_docker_helper.config.planning.local_tree import local_tree_digest
from comfyui_docker_helper.config.runtime.config import load_runtime_config
from comfyui_docker_helper.filesystem import admission as file_admission
from comfyui_docker_helper.release_artifacts import (
    WORKSPACE_PROFILE_CONTEXT_PATH,
    WORKSPACE_PROFILE_RESOURCE,
    CanonicalWheel,
)
from comfyui_docker_helper.rendering import final_materializer as materializer_module
from comfyui_docker_helper.rendering.final_materializer import (
    FinalMaterializationError,
    LocalMaterializationSource,
    _materialize_private_stage,
)
from comfyui_docker_helper.rendering.final_renderer import (
    render_build_plan_dockerfile,
)

_VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "first@example"
)


# Rendering consumes literal BuildPlan inputs and materializes them without replanning.
def test_renderer_uses_only_literal_digest_qualified_from_references() -> None:
    plan = build_plan(final_config(), accepted_resolution())

    rendered = render_build_plan_dockerfile(plan)

    assert rendered.count("FROM ") == 2
    assert (
        f"FROM --platform=linux/amd64 {plan.toolchain.uv_image.reference} AS uv"
        in rendered
    )
    assert (
        f"FROM --platform=linux/amd64 {plan.toolchain.cuda_image.reference}\n"
        in rendered
    )
    assert (
        "COPY --from=uv /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/"
        in rendered
    )
    assert "ARG " not in rendered
    assert "\nCMD " not in rendered
    assert "${PATH}" in rendered
    instructions = rendered.splitlines()
    assert [line for line in instructions if line.startswith("STOPSIGNAL ")] == [
        "STOPSIGNAL SIGTERM"
    ]
    assert [line for line in instructions if line.startswith("ENTRYPOINT ")] == [
        'ENTRYPOINT ["/usr/bin/tini", "--", "/opt/uv/bin/cdh", '
        '"container", "runtime", "serve"]'
    ]
    assert instructions[-2:] == [
        "STOPSIGNAL SIGTERM",
        'ENTRYPOINT ["/usr/bin/tini", "--", "/opt/uv/bin/cdh", '
        '"container", "runtime", "serve"]',
    ]
    assert "test -x /usr/bin/tini" not in rendered
    assert plan.application.os_packages.count("tini") == 1
    assert plan.application.os_packages.count("tzdata") == 1
    assert (
        rendered.splitlines().count(
            "COPY --chmod=0644 runtime/cdh-workspace.sh /etc/profile.d/cdh-workspace.sh"
        )
        == 1
    )
    assert rendered == render_build_plan_dockerfile(plan)


def test_renderer_mounts_build_plan_only_for_its_build_consumers() -> None:
    plan = build_plan(final_config(), accepted_resolution())

    rendered = render_build_plan_dockerfile(plan)
    plan_mount = (
        "--mount=type=bind,source=build-plan.json,"
        "target=/opt/cdh/build/build-plan.json,readonly"
    )
    consumer_markers = (
        "container install-comfyui",
        "container install-custom-nodes",
        "container download-files",
        "container emit-final-manifest",
    )

    assert "COPY --chmod=0644 build-plan.json" not in rendered
    assert rendered.count("RUN mkdir -p /opt/cdh/build") == 1
    assert rendered.count(plan_mount) == len(consumer_markers)
    plan_digest_argument = f"--build-plan-digest {build_plan_digest(plan)}"
    for marker in consumer_markers:
        block = next(item for item in _run_blocks(rendered) if marker in item)
        assert block.count(plan_mount) == 1
        assert _flatten_command(block).count(plan_digest_argument) == 1

    document = final_config().model_dump(mode="python")
    document["files"] = []
    without_files = render_build_plan_dockerfile(
        build_plan(FinalConfig.model_validate(document), accepted_resolution())
    )
    assert "container download-files" not in without_files
    assert without_files.count(plan_mount) == len(consumer_markers) - 1


def test_renderer_labels_stable_semantic_phases_in_build_order() -> None:
    plan = build_plan(
        final_config(with_uv_tool=True), accepted_resolution(with_uv_tool=True)
    )

    rendered = render_build_plan_dockerfile(plan)
    headings = (
        "Base images and runtime inputs",
        "OS packages",
        "Managed Python and application environment",
        "Canonical cdh",
        "Isolated tools",
        "ComfyUI",
        "Custom nodes",
        "Copied files",
        "Final verification",
        "Runtime entrypoint",
    )
    positions = tuple(rendered.index(f"# {heading}") for heading in headings)

    assert positions == tuple(sorted(positions))
    assert all(rendered.count(f"# {heading}") == 1 for heading in headings)
    assert all(rendered[position - 2 : position] == "\n\n" for position in positions)


# Build caches stay outside image layers, and package-generated SSH identity is removed.
def test_renderer_scopes_package_caches_and_ssh_key_cleanup_to_owning_runs() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    rendered = render_build_plan_dockerfile(plan)
    run_blocks = tuple(f"RUN {block}" for block in rendered.split("\nRUN ")[1:])
    apt_block = next(block for block in run_blocks if "apt-get update" in block)

    assert apt_block.startswith(
        "RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \\\n"
        "    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked"
    )
    assert apt_block.index("apt-get install") < apt_block.index(
        "rm -f /etc/ssh/ssh_host_*"
    )
    assert apt_block.splitlines().count("        tzdata \\") == 1
    uv_blocks = tuple(
        block
        for block in run_blocks
        if "--mount=type=cache,target=/root/.cache/uv" in block
    )
    assert uv_blocks
    assert all("sharing=" not in block for block in uv_blocks)

    bootstrap = next(
        block for block in uv_blocks if "uv --no-config python install" in block
    )
    bootstrap_command = _flatten_command(bootstrap)
    assert "export UV_CACHE_DIR=/root/.cache/uv UV_LINK_MODE=copy" in bootstrap
    assert (
        "UV_PYTHON_CACHE_DIR=/root/.cache/uv/python uv --no-config python install"
        in bootstrap_command
    )
    python_cache_assignment = "UV_PYTHON_CACHE_DIR=/root/.cache/uv/python"
    assert rendered.count(python_cache_assignment) == 1
    assert bootstrap.count(python_cache_assignment) == 1
    assert all(
        python_cache_assignment not in block
        for block in uv_blocks
        if block != bootstrap
    )
    for variable in ("UV_CACHE_DIR", "UV_PYTHON_CACHE_DIR", "UV_LINK_MODE"):
        assert f"ENV {variable}=" not in rendered

    cdh_block = next(block for block in uv_blocks if "source=bootstrap/" in block)
    cdh_command = _flatten_command(cdh_block)
    assert "export UV_CACHE_DIR" not in cdh_block
    assert (
        "UV_CACHE_DIR=/root/.cache/uv UV_LINK_MODE=copy uv --no-config tool install"
        in cdh_command
    )

    application_block = next(
        block for block in uv_blocks if "container install-comfyui" in block
    )
    assert "export UV_CACHE_DIR" not in application_block

    custom_node_block = next(
        block for block in uv_blocks if "container install-custom-nodes" in block
    )
    assert "export UV_CACHE_DIR=/root/.cache/uv UV_LINK_MODE=copy" in custom_node_block

    final_observer = next(
        block for block in uv_blocks if "container emit-final-manifest" in block
    )
    assert "export UV_CACHE_DIR" not in final_observer
    assert "UV_LINK_MODE" not in final_observer

    wheel_block = next(item for item in run_blocks if "source=bootstrap/" in item)
    wheel = plan.toolchain.tool_store.cdh
    wheel_filename = f"comfyui_docker_helper-{wheel.version}-py3-none-any.whl"
    wheel_digest_check = (
        f"test \"$(sha256sum /tmp/{wheel_filename} | cut -d ' ' -f 1)\" = "
        f"{wheel.wheel_digest.removeprefix('sha256:')}"
    )
    wheel_command = _flatten_command(wheel_block)
    assert wheel_command.count(wheel_digest_check) == 1
    assert wheel_command.index(wheel_digest_check) < wheel_command.index(
        "UV_CACHE_DIR=/root/.cache/uv UV_LINK_MODE=copy"
    )
    assert wheel_block.startswith(
        "RUN --mount=type=cache,target=/root/.cache/uv \\\n"
        "    --mount=type=bind,source=bootstrap/"
    )
    assert wheel_block.index("--mount=type=bind") < wheel_command.index(
        "UV_CACHE_DIR=/root/.cache/uv UV_LINK_MODE=copy"
    )


# Renderer-owned caches, quoted container paths, runtime environment, and
# isolated tools remain independent of user runtime paths and modes.
def test_renderer_keeps_build_uv_cache_outside_user_runtime_cache_paths() -> None:
    config = final_config()
    document = config.model_dump(mode="python")
    document["system"]["env"].update(
        {"HOME": "/workspace/home", "XDG_CACHE_HOME": "/workspace/cache"}
    )
    plan = build_plan(FinalConfig.model_validate(document), accepted_resolution())

    rendered = render_build_plan_dockerfile(plan)

    assert 'ENV HOME="/workspace/home"' in rendered
    assert 'ENV XDG_CACHE_HOME="/workspace/cache"' in rendered
    uv_run_blocks = tuple(
        f"RUN {block}"
        for block in rendered.split("\nRUN ")[1:]
        if "--mount=type=cache,target=/root/.cache/uv" in block
    )
    assert uv_run_blocks
    assert all("sharing=" not in block for block in uv_run_blocks)
    assert any(
        "export UV_CACHE_DIR=/root/.cache/uv UV_LINK_MODE=copy" in block
        for block in uv_run_blocks
    )
    assert all(
        "UV_CACHE_DIR=/workspace/cache" not in block
        and "UV_PYTHON_CACHE_DIR=/workspace/cache" not in block
        for block in uv_run_blocks
    )


def test_renderer_quotes_container_paths_without_host_projection() -> None:
    config = final_config()
    document = config.model_dump(mode="python")
    document["system"]["workspace"] = "/workspace data"
    changed = FinalConfig.model_validate(document)

    plan = build_plan(changed, accepted_resolution())
    rendered = render_build_plan_dockerfile(plan)
    launch_comfyui = PurePosixPath(plan.runtime.launch_command[1]).parent

    assert 'ENV WORKSPACE="/workspace data"' in rendered
    assert 'WORKDIR "/workspace data"' in rendered
    assert f"ENV COMFYUI_PATH={json.dumps(str(launch_comfyui))}" in rendered
    assert 'ENV VIRTUAL_ENV="/opt/venv"' in rendered
    assert 'ENV PATH="/opt/uv/bin:/opt/venv/bin:${PATH}"' in rendered


def test_renderer_preserves_non_package_runtime_environment() -> None:
    rendered = render_build_plan_dockerfile(
        build_plan(final_config(), accepted_resolution())
    )

    assert 'ENV ALPHA="first"' in rendered
    assert 'ENV ZED="last"' in rendered


def test_renderer_projects_configured_timezone_as_ordinary_runtime_environment() -> (
    None
):
    document = final_config().model_dump(mode="python")
    document["system"]["env"]["TZ"] = "Asia/Shanghai"
    plan = build_plan(FinalConfig.model_validate(document), accepted_resolution())

    rendered = render_build_plan_dockerfile(plan)

    assert rendered.splitlines().count('ENV TZ="Asia/Shanghai"') == 1


def test_renderer_omits_unconfigured_timezone_environment() -> None:
    rendered = render_build_plan_dockerfile(
        build_plan(final_config(), accepted_resolution())
    )

    assert all(not line.startswith("ENV TZ=") for line in rendered.splitlines())


def test_renderer_installs_isolated_comfy_cli_before_generic_tools() -> None:
    plan = build_plan(
        final_config(with_uv_tool=True), accepted_resolution(with_uv_tool=True)
    )

    rendered = render_build_plan_dockerfile(plan)

    cdh_install = rendered.index(f"source=bootstrap/{canonical_wheel().filename}")
    cli_install = rendered.index("comfy-cli==1.8.0")
    generic_install = rendered.index("ruff==0.15.18")
    application_install = rendered.index("container install-comfyui")
    assert cdh_install < cli_install < generic_install < application_install
    cli_run_block = next(
        block for block in _run_blocks(rendered) if "comfy-cli==1.8.0" in block
    )
    cli_command_install = cli_run_block.index("UV_CACHE_DIR=/root/.cache/uv")
    for command in ("comfy", "comfy-cli", "comfycli"):
        collision_preflight = (
            f"test ! -e /opt/uv/bin/{command} && test ! -L /opt/uv/bin/{command}"
        )
        assert cli_run_block.count(collision_preflight) == 1
        assert cli_run_block.index(collision_preflight) < cli_command_install
    cdh_block = rendered[cdh_install:cli_install]
    assert "uv --no-config pip check" not in cdh_block
    assert "--force" not in rendered
    cli_block = rendered[cli_install:generic_install]
    assert "--with" not in cli_block
    assert "UV_CONSTRAINT" not in cli_block
    assert "PIP_CONSTRAINT" not in cli_block
    assert "/opt/venv" not in cli_block
    assert "sys._base_executable" not in cli_block
    assert "console_scripts" not in cli_block
    assert "uv --no-config pip check" not in cli_block
    assert "test -x /opt/uv/tools/comfy-cli/bin/python" not in cli_block
    assert (
        "UV_CACHE_DIR=/root/.cache/uv UV_LINK_MODE=copy uv --no-config tool install"
        in _flatten_command(cli_block)
    )
    for command in ("comfy", "comfy-cli", "comfycli"):
        assert f"/opt/uv/bin/{command}" in rendered
        assert f"/opt/uv/tools/comfy-cli/bin/{command}" not in cli_block
    assert " --help" not in rendered
    assert 'UV_TOOL_DIR="/opt/uv/tools"' in rendered
    assert 'UV_TOOL_BIN_DIR="/opt/uv/bin"' in rendered
    assert 'ENV PATH="/opt/uv/bin:/opt/venv/bin:$' + '{PATH}"' in rendered
    assert plan.runtime.launch_command[0] == "/opt/venv/bin/python"
    assert "uv --no-config pip check" not in rendered


def test_renderer_installs_uv_tool_from_authored_direct_requirement() -> None:
    source = "git+https://example.test/ruff.git@main"
    document = build_plan(
        final_config(with_uv_tool=True), accepted_resolution(with_uv_tool=True)
    ).model_dump(mode="python")
    document["toolchain"]["tool_store"]["uv_tools"][0]["direct_reference"] = source
    plan = BuildPlan.model_validate(document)
    tool = plan.toolchain.tool_store.uv_tools[0]

    rendered = render_build_plan_dockerfile(plan)

    block = next(item for item in _run_blocks(rendered) if source in item)
    tokens = shlex.split(block.replace("\\\n", " "))
    assert tokens.count(tool.requirement) == 1
    assert tokens[tokens.index("--default-index") + 1] == (
        plan.application.python_index_url
    )
    assert "importlib.metadata as m;" not in tokens
    assert "UV_CACHE_DIR=/root/.cache/uv" in tokens
    assert "UV_LINK_MODE=copy" in tokens


def test_renderer_disabled_mode_reserves_no_comfy_cli_commands() -> None:
    rendered = render_build_plan_dockerfile(
        build_plan(
            final_config(install_cli=False), accepted_resolution(install_cli=False)
        )
    )

    assert "uv --no-config tool install" in rendered  # cdh remains a uv tool.
    assert "comfy-cli==" not in rendered
    for command in ("comfy", "comfy-cli", "comfycli"):
        assert f"test ! -e /opt/uv/bin/{command}" not in rendered
        assert f"test ! -L /opt/uv/bin/{command}" not in rendered


def test_renderer_omits_build_download_command_when_no_files() -> None:
    document = final_config().model_dump(mode="python")
    document["files"] = []
    plan = build_plan(FinalConfig.model_validate(document), accepted_resolution())

    rendered = render_build_plan_dockerfile(plan)
    assert "container download-files" not in rendered
    assert "container validate-local-trees" not in rendered
    assert "container normalize-local-trees" not in rendered


def test_renderer_places_local_files_authoritatively_after_build_mutations() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    relative_target = "models/checkpoints/model [final].bin"
    context_path = (
        "build/files/" + hashlib.sha256(relative_target.encode("utf-8")).hexdigest()
    )
    document["files"]["files"] = (
        *document["files"]["files"],
        {
            "type": "local",
            "kind": "file",
            "target": f"{plan.application.paths.comfyui}/{relative_target}",
            "context_path": context_path,
            "verification": "unverified-local",
        },
    )
    changed = BuildPlan.model_validate(document)

    rendered = render_build_plan_dockerfile(changed)
    copy_line = "COPY --link --chmod=0644 " + json.dumps(
        [context_path, f"{plan.application.paths.comfyui}/{relative_target}"]
    )

    assert copy_line in rendered
    assert rendered.index("container install-custom-nodes") < rendered.index(copy_line)
    assert rendered.index("container download-files") < rendered.index(copy_line)
    assert rendered.index(copy_line) < rendered.index("container emit-final-manifest")


def test_renderer_escapes_dollar_in_local_file_copy_destination_only() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    relative_target = "models/$NAME/${OTHER}/model.bin"
    context_path = (
        "build/files/" + hashlib.sha256(relative_target.encode("utf-8")).hexdigest()
    )
    document = plan.model_dump(mode="python")
    document["files"]["files"] = (
        *document["files"]["files"],
        {
            "type": "local",
            "kind": "file",
            "target": f"{plan.application.paths.comfyui}/{relative_target}",
            "context_path": context_path,
            "verification": "unverified-local",
        },
    )
    changed = BuildPlan.model_validate(document)

    rendered = render_build_plan_dockerfile(changed)
    copy_line = "COPY --link --chmod=0644 " + json.dumps(
        [
            context_path,
            f"{plan.application.paths.comfyui}/{relative_target}".replace("$", "\\$"),
        ]
    )

    assert copy_line in rendered
    assert "container validate-local-trees" not in rendered
    assert 'ENV PATH="/opt/uv/bin:/opt/venv/bin:${PATH}"' in rendered


def test_renderer_places_one_copy_per_tree_then_one_tree_normalizer() -> None:
    plan, context_path = _plan_with_local_tree()
    document = plan.model_dump(mode="python")
    relative_file_target = "models/local.bin"
    file_context_path = (
        "build/files/"
        + hashlib.sha256(relative_file_target.encode("utf-8")).hexdigest()
    )
    file_target = f"{plan.application.paths.comfyui}/{relative_file_target}"
    empty_relative_target = "models/empty-tree"
    empty_context_path = (
        "build/trees/"
        + hashlib.sha256(empty_relative_target.encode("utf-8")).hexdigest()
    )
    document["files"]["files"] = (
        {
            "type": "local",
            "kind": "file",
            "target": file_target,
            "context_path": file_context_path,
            "verification": "unverified-local",
        },
        *document["files"]["files"],
        {
            "type": "local",
            "kind": "tree",
            "target": f"{plan.application.paths.comfyui}/{empty_relative_target}",
            "context_path": empty_context_path,
            "verification": "unverified-local",
            "members": (),
            "tree_digest": None,
        },
    )
    plan = BuildPlan.model_validate(document)

    rendered = render_build_plan_dockerfile(plan)
    copy_lines = (
        "COPY --link --chmod=0644 " + json.dumps([file_context_path, file_target]),
        "COPY --link "
        + json.dumps(
            [
                f"{context_path}/",
                f"{plan.application.paths.comfyui}/user/default/workflows/",
            ]
        ),
        "COPY --link "
        + json.dumps(
            [
                f"{empty_context_path}/",
                f"{plan.application.paths.comfyui}/{empty_relative_target}/",
            ]
        ),
    )
    normalizer_blocks = tuple(
        block for block in _run_blocks(rendered) if "normalize-local-trees" in block
    )
    validator_blocks = tuple(
        block for block in _run_blocks(rendered) if "validate-local-trees" in block
    )

    assert all(rendered.count(copy_line) == 1 for copy_line in copy_lines)
    assert rendered.count("validate-local-trees") == 1
    assert rendered.count("normalize-local-trees") == 1
    assert len(validator_blocks) == 1
    validator = validator_blocks[0]
    assert (
        validator.count(
            "--mount=type=bind,source=build-plan.json,"
            "target=/opt/cdh/build/build-plan.json,readonly"
        )
        == 1
    )
    assert f"--build-plan-digest {build_plan_digest(plan)}" in _flatten_command(
        validator
    )
    assert len(normalizer_blocks) == 1
    normalizer = normalizer_blocks[0]
    assert (
        normalizer.count(
            "--mount=type=bind,source=build-plan.json,"
            "target=/opt/cdh/build/build-plan.json,readonly"
        )
        == 1
    )
    assert f"--build-plan-digest {build_plan_digest(plan)}" in _flatten_command(
        normalizer
    )
    assert rendered.index(copy_lines[1]) < rendered.index(copy_lines[2])
    validator_index = rendered.index("validate-local-trees")
    normalizer_index = rendered.index("normalize-local-trees")
    assert validator_index < min(rendered.index(copy_line) for copy_line in copy_lines)
    assert max(rendered.index(copy_line) for copy_line in copy_lines) < normalizer_index
    assert rendered.index("normalize-local-trees") < rendered.index(
        "container emit-final-manifest"
    )


def test_renderer_escapes_dollar_in_local_tree_copy_destination() -> None:
    relative_target = "user/$NAME/${OTHER}/workflows"
    plan, context_path = _plan_with_local_tree(relative_target=relative_target)

    rendered = render_build_plan_dockerfile(plan)
    copy_line = "COPY --link " + json.dumps(
        [
            f"{context_path}/",
            f"{plan.application.paths.comfyui}/{relative_target}".replace("$", "\\$")
            + "/",
        ]
    )

    assert rendered.count(copy_line) == 1
    assert rendered.count("container validate-local-trees") == 1
    assert rendered.count("container normalize-local-trees") == 1


# Custom-node and application modes render one ordered observed execution boundary.
def test_renderer_runs_complete_custom_node_sequence_in_one_later_layer() -> None:
    plan = build_plan(
        final_config(install_cli=False), accepted_resolution(install_cli=False)
    )

    rendered = render_build_plan_dockerfile(plan)

    assert rendered.count("container install-custom-nodes") == 1
    assert rendered.index("container install-comfyui") < rendered.index(
        "container install-custom-nodes"
    )
    custom_node_block = next(
        block for block in _run_blocks(rendered) if "install-custom-nodes" in block
    )
    assert custom_node_block.startswith(
        "RUN --mount=type=cache,target=/root/.cache/uv \\\n"
        "    --mount=type=ssh,id=default,required=false"
    )
    assert "export UV_CACHE_DIR=/root/.cache/uv UV_LINK_MODE=copy" in custom_node_block
    assert "GIT_SSH_COMMAND=" in custom_node_block
    assert custom_node_block.index("export UV_CACHE_DIR") < custom_node_block.index(
        "GIT_SSH_COMMAND="
    )
    assert f"--build-plan-digest {build_plan_digest(plan)}" in _flatten_command(
        custom_node_block
    )
    assert (
        "--constraints /opt/cdh/build/python-package-constraints.txt"
        in _flatten_command(custom_node_block)
    )
    assert "--build-hooks-directory /opt/cdh/build/hooks" in _flatten_command(
        custom_node_block
    )
    assert rendered.index("container install-custom-nodes") < rendered.index(
        "container download-files"
    )
    assert rendered.index("container download-files") < rendered.index(
        "container emit-final-manifest"
    )
    assert "comfy node" not in rendered
    assert "comfy install" not in rendered


# Default OpenSSH trust sources map to stable identities shared by host and
# renderer.
def test_known_hosts_mount_descriptors_define_the_public_mapping() -> None:
    expected = (
        (
            "cdh-ssh-known-hosts-user",
            "/run/secrets/cdh-ssh-known-hosts-user",
            "~/.ssh/known_hosts",
            "user",
        ),
        (
            "cdh-ssh-known-hosts-user-legacy",
            "/run/secrets/cdh-ssh-known-hosts-user-legacy",
            "~/.ssh/known_hosts2",
            "user",
        ),
        (
            "cdh-ssh-known-hosts-system",
            "/run/secrets/cdh-ssh-known-hosts-system",
            "/etc/ssh/ssh_known_hosts",
            "system",
        ),
        (
            "cdh-ssh-known-hosts-system-legacy",
            "/run/secrets/cdh-ssh-known-hosts-system-legacy",
            "/etc/ssh/ssh_known_hosts2",
            "system",
        ),
    )
    assert expected == KNOWN_HOSTS_MOUNTS


# Direct-Git plans alone receive optional, strict, non-interactive SSH inputs.
@pytest.mark.parametrize(
    ("node_types", "uses_ssh"),
    [
        (("git",), True),
        (("registry", "git"), True),
        (("registry",), False),
        ((), False),
    ],
)
def test_renderer_scopes_strict_optional_ssh_mounts_to_direct_git_plans(
    node_types: tuple[str, ...],
    uses_ssh: bool,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["custom_nodes"]["nodes"] = tuple(
        node for node in document["custom_nodes"]["nodes"] if node["type"] in node_types
    )
    changed = BuildPlan.model_validate(document)

    rendered = render_build_plan_dockerfile(changed)
    custom_node_block = next(
        block for block in _run_blocks(rendered) if "install-custom-nodes" in block
    )

    assert rendered.count("container install-custom-nodes") == 1
    ssh_mount = "--mount=type=ssh,id=default,required=false"
    secret_mounts = tuple(
        "--mount=type=secret,"
        f"id={descriptor.secret_id},target={descriptor.target},required=false"
        for descriptor in KNOWN_HOSTS_MOUNTS
    )
    user_paths = " ".join(
        descriptor.target
        for descriptor in KNOWN_HOSTS_MOUNTS
        if descriptor.scope == "user"
    )
    system_paths = " ".join(
        descriptor.target
        for descriptor in KNOWN_HOSTS_MOUNTS
        if descriptor.scope == "system"
    )
    ssh_command = (
        "/usr/bin/ssh -F none "
        "-o BatchMode=yes "
        "-o StrictHostKeyChecking=yes "
        "-o KnownHostsCommand=none "
        f'-o UserKnownHostsFile="{user_paths}" '
        f'-o GlobalKnownHostsFile="{system_paths}"'
    )

    if uses_ssh:
        assert custom_node_block.count(ssh_mount) == 1
        for secret_mount in secret_mounts:
            assert custom_node_block.count(secret_mount) == 1
        assert f"GIT_SSH_COMMAND={shlex.quote(ssh_command)}" in custom_node_block
        assert custom_node_block.index(ssh_mount) < custom_node_block.index(
            secret_mounts[0]
        )
        assert custom_node_block.index(secret_mounts[-1]) < custom_node_block.index(
            "export UV_CACHE_DIR"
        )
    else:
        assert ssh_mount not in custom_node_block
        assert all(mount not in custom_node_block for mount in secret_mounts)
        assert "GIT_SSH_COMMAND" not in custom_node_block


def test_renderer_mounts_distinct_git_credentials_as_required_fixed_targets() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    routes = (
        GitCredentialRoutePlan(
            match="https://example.test/team",
            username="first",
            secret_id="cdh-git-credential-shared",
        ),
        GitCredentialRoutePlan(
            match="https://example.test/team/subgroup",
            username="second",
            secret_id="cdh-git-credential-shared",
        ),
        GitCredentialRoutePlan(
            match="https://git.example.test/other",
            username="third",
            secret_id="cdh-git-credential-other",
        ),
    )
    plan = plan.model_copy(
        update={
            "custom_nodes": plan.custom_nodes.model_copy(
                update={"git_credentials": routes}
            )
        }
    )

    rendered = render_build_plan_dockerfile(plan)
    block = next(
        item for item in _run_blocks(rendered) if "install-custom-nodes" in item
    )
    shared = (
        "--mount=type=secret,id=cdh-git-credential-shared,"
        "target=/run/secrets/cdh-git-credential-shared,required=true"
    )
    other = (
        "--mount=type=secret,id=cdh-git-credential-other,"
        "target=/run/secrets/cdh-git-credential-other,required=true"
    )

    assert block.count(shared) == 1
    assert block.count(other) == 1
    assert block.index(shared) < block.index(other)
    assert "CDH_PRIVATE_TOKEN" not in rendered
    assert "/host/source" not in rendered

    registry_only = plan.model_copy(
        update={
            "custom_nodes": plan.custom_nodes.model_copy(
                update={
                    "nodes": tuple(
                        node
                        for node in plan.custom_nodes.nodes
                        if node.type == "registry"
                    )
                }
            )
        }
    )
    registry_block = next(
        item
        for item in _run_blocks(render_build_plan_dockerfile(registry_only))
        if "install-custom-nodes" in item
    )
    assert "cdh-git-credential-" not in registry_block


def test_renderer_mounts_distinct_downloader_credentials_only_on_httpx_download() -> (
    None
):
    plan = build_plan(final_config(), accepted_resolution())
    routes = (
        DownloaderCredentialRoutePlan(
            match="https://example.test/",
            type="bearer",
            token={"secret": "shared"},
            secret_id="cdh-downloader-credential-shared",
        ),
        DownloaderCredentialRoutePlan(
            match="https://example.test/private",
            type="bearer",
            token={"secret": "shared"},
            secret_id="cdh-downloader-credential-shared",
        ),
        DownloaderCredentialRoutePlan(
            match="https://cdn.example.test/",
            type="bearer",
            token={"secret": "cdn"},
            secret_id="cdh-downloader-credential-cdn",
        ),
    )
    httpx_files = tuple(
        item.model_copy(update={"downloader": "httpx"}) if item.type == "http" else item
        for item in plan.files.files
    )
    plan = plan.model_copy(
        update={
            "files": plan.files.model_copy(
                update={"credentials": routes, "files": httpx_files}
            )
        }
    )

    blocks = _run_blocks(render_build_plan_dockerfile(plan))
    download = next(block for block in blocks if "download-files" in block)
    shared = (
        "--mount=type=secret,id=cdh-downloader-credential-shared,"
        "target=/run/secrets/cdh-downloader-credential-shared,required=true"
    )
    cdn = (
        "--mount=type=secret,id=cdh-downloader-credential-cdn,"
        "target=/run/secrets/cdh-downloader-credential-cdn,required=true"
    )

    assert download.count(shared) == 1
    assert download.count(cdn) == 1
    assert download.index(shared) < download.index(cdn)
    assert all(
        "cdh-downloader-credential-" not in block
        for block in blocks
        if "download-files" not in block
    )

    aria2_plan = plan.model_copy(
        update={
            "files": plan.files.model_copy(
                update={
                    "credentials": routes[2:],
                    "files": tuple(
                        item.model_copy(update={"downloader": "aria2"})
                        if item.type == "http"
                        else item
                        for item in plan.files.files
                    ),
                }
            )
        }
    )
    aria2_download = next(
        block
        for block in _run_blocks(render_build_plan_dockerfile(aria2_plan))
        if "download-files" in block
    )
    assert "cdh-downloader-credential-" not in aria2_download


# Materialization writes one deterministic BuildPlan and verified local inputs.
def _plan_with_local_file(*, digest: str | None = None) -> tuple[BuildPlan, str]:
    plan = build_plan(final_config(), accepted_resolution())
    relative_target = "models/checkpoints/local-model.bin"
    context_path = (
        f"build/files/{hashlib.sha256(relative_target.encode('utf-8')).hexdigest()}"
    )
    local = LocalFilePlan(
        type="local",
        kind="file",
        target=f"/workspace/ComfyUI/{relative_target}",
        context_path=context_path,
        verification="sha256" if digest is not None else "unverified-local",
        digest=digest,
    )
    return (
        plan.model_copy(
            update={"files": plan.files.model_copy(update={"files": (local,)})}
        ),
        context_path,
    )


def _plan_with_local_tree(
    *,
    locked: bool = False,
    relative_target: str = "user/default/workflows",
) -> tuple[BuildPlan, str]:
    plan = build_plan(final_config(), accepted_resolution())
    context_path = (
        f"build/trees/{hashlib.sha256(relative_target.encode('utf-8')).hexdigest()}"
    )
    members = (
        LocalTreeMemberPlan(
            relative_path=".hidden",
            kind="directory",
            size=None,
            digest=None,
        ),
        LocalTreeMemberPlan(
            relative_path=".hidden/empty",
            kind="directory",
            size=None,
            digest=None,
        ),
        LocalTreeMemberPlan(
            relative_path=".hidden/payload.bin",
            kind="file",
            size=6 if locked else None,
            digest=(
                f"sha256:{hashlib.sha256(b'hidden').hexdigest()}" if locked else None
            ),
        ),
        LocalTreeMemberPlan(
            relative_path="nested",
            kind="directory",
            size=None,
            digest=None,
        ),
        LocalTreeMemberPlan(
            relative_path="nested/payload.bin",
            kind="file",
            size=6 if locked else None,
            digest=(
                f"sha256:{hashlib.sha256(b'nested').hexdigest()}" if locked else None
            ),
        ),
    )
    tree_digest = None
    if locked:
        tree_digest = local_tree_digest(
            file_admission.LocalTreeInventory(
                (
                    file_admission.LocalTreeMember(".hidden", "directory"),
                    file_admission.LocalTreeMember(".hidden/empty", "directory"),
                    file_admission.LocalTreeMember(
                        ".hidden/payload.bin",
                        "file",
                        6,
                        f"sha256:{hashlib.sha256(b'hidden').hexdigest()}",
                    ),
                    file_admission.LocalTreeMember("nested", "directory"),
                    file_admission.LocalTreeMember(
                        "nested/payload.bin",
                        "file",
                        6,
                        f"sha256:{hashlib.sha256(b'nested').hexdigest()}",
                    ),
                )
            )
        )
    local = LocalTreePlan(
        type="local",
        kind="tree",
        target=f"{plan.application.paths.comfyui}/{relative_target}",
        context_path=context_path,
        verification="sha256" if locked else "unverified-local",
        members=members,
        tree_digest=tree_digest,
    )
    return (
        plan.model_copy(
            update={"files": plan.files.model_copy(update={"files": (local,)})}
        ),
        context_path,
    )


def test_materializer_writes_deterministic_plan_and_verified_input(
    tmp_path: Path,
) -> None:
    content = b"#!/usr/bin/env python3\n"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    build_hooks = tmp_path / "build_hooks"
    source = build_hooks / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(build_hooks_dir=build_hooks, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    source_input = LocalMaterializationSource(
        PurePosixPath("build-hooks/hooks/pre.py"), source, kind="file"
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)

    _materialize_private_stage(
        plan,
        first,
        canonical_wheel=canonical_wheel(),
        local_sources=(source_input,),
    )
    _materialize_private_stage(
        plan,
        second,
        canonical_wheel=canonical_wheel(),
        local_sources=(source_input,),
    )

    assert (first / "build-plan.json").read_bytes() == dump_build_plan_json(plan)
    assert (first / "build/hooks/hooks/pre.py").read_bytes() == content
    assert (
        first / WORKSPACE_PROFILE_CONTEXT_PATH
    ).read_bytes() == CANONICAL_WORKSPACE_PROFILE_CONTENT
    assert (
        WORKSPACE_PROFILE_RESOURCE.read_bytes() != CANONICAL_WORKSPACE_PROFILE_CONTENT
    )
    assert (first / "Dockerfile").read_text() == render_build_plan_dockerfile(plan)
    assert (
        "COPY --chmod=0644 runtime/config.toml /opt/cdh/runtime/config.toml"
        in (first / "Dockerfile").read_text()
    )
    runtime = load_runtime_config(
        baked_config_path=first / "runtime/config.toml",
        mounted_config_path=tmp_path / "missing-runtime.toml",
        environ={},
    )
    assert runtime.config.comfyui.port == 8188
    assert _tree(first) == _tree(second)
    tree = _tree(first)
    wheel = canonical_wheel()
    assert tree[f"bootstrap/{wheel.filename}"] == wheel.content
    assert tree[".dockerignore"] == b"/.cdh-rendered\n/config.lock.toml\n"
    assert not (first / "config.toml").exists()
    assert not (first / "config.lock.toml").exists()
    assert str(source).encode() not in (first / "build-plan.json").read_bytes()

    dockerfile = (first / "Dockerfile").read_text()
    assert "COPY --chmod=0644 build-plan.json" not in dockerfile
    assert dockerfile.count("RUN mkdir -p /opt/cdh/build") == 1
    assert "COPY --chmod=0755 build/hooks /opt/cdh/build/hooks" in dockerfile
    assert (
        f"--mount=type=bind,source=bootstrap/{wheel.filename},"
        f"target=/tmp/{wheel.filename},readonly" in dockerfile
    )
    assert "COPY bootstrap" not in dockerfile
    assert "container install-comfyui" in dockerfile
    for inline_probe in (
        "importlib.metadata as m",
        "platform.python_version()",
        "sys._base_executable",
        "console_scripts",
    ):
        assert inline_probe not in dockerfile
    assert plan.application.pip_version in dockerfile
    assert "torch==2.12.1+cu130" not in dockerfile
    assert "UV_CONSTRAINT" not in dockerfile
    assert "PIP_CONSTRAINT" not in dockerfile

    assert parse_build_plan_json((first / "build-plan.json").read_bytes()) == plan


@pytest.mark.parametrize("locked", [False, True])
def test_materializer_writes_one_complete_local_tree_context(
    tmp_path: Path,
    locked: bool,
) -> None:
    source = tmp_path / "tree"
    (source / ".hidden" / "empty").mkdir(parents=True)
    (source / ".hidden" / "payload.bin").write_bytes(b"hidden")
    (source / "nested").mkdir()
    (source / "nested" / "payload.bin").write_bytes(b"nested")
    plan, context_path = _plan_with_local_tree(locked=locked)
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)

    _materialize_private_stage(
        plan,
        stage,
        canonical_wheel=canonical_wheel(),
        local_sources=(
            LocalMaterializationSource(
                PurePosixPath(context_path), source, kind="tree"
            ),
        ),
        local_file_mode="copy",
    )

    context = stage / context_path
    assert context.is_dir()
    assert (context / ".hidden" / "payload.bin").read_bytes() == b"hidden"
    assert (context / "nested" / "payload.bin").read_bytes() == b"nested"
    assert (context / ".hidden" / "empty").is_dir()
    assert not (context / "tree").exists()
    if os.name == "posix":
        assert context.stat().st_mode & 0o777 == 0o755
        assert (context / ".hidden").stat().st_mode & 0o777 == 0o755
        assert (context / ".hidden" / "payload.bin").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "drift_phase",
    ["before", "during"],
    ids=["before-materialization", "during-materialization"],
)
def test_materializer_maps_tree_membership_drift_to_its_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_phase: str,
) -> None:
    source = tmp_path / "tree"
    (source / ".hidden" / "empty").mkdir(parents=True)
    (source / ".hidden" / "payload.bin").write_bytes(b"hidden")
    (source / "nested").mkdir()
    (source / "nested" / "payload.bin").write_bytes(b"nested")
    plan, context_path = _plan_with_local_tree()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    drift = file_admission.TreeAdmissionError(
        "synthetic membership drift",
        code="membership_drift",
    )
    outcomes: list[file_admission.TreeAdmissionError | None] = (
        [drift] if drift_phase == "before" else [None, drift]
    )

    def revalidate(
        _source: Path,
        expected: file_admission.LocalTreeInventory,
    ) -> file_admission.LocalTreeInventory:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return expected

    monkeypatch.setattr(materializer_module, "revalidate_local_tree", revalidate)

    with pytest.raises(
        FinalMaterializationError,
        match=f"changed {drift_phase} materialization",
    ):
        _materialize_private_stage(
            plan,
            stage,
            canonical_wheel=canonical_wheel(),
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath(context_path), source, kind="tree"
                ),
            ),
            local_file_mode="copy",
        )


def test_materializer_requires_tree_source_kind_to_match_plan(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    plan, context_path = _plan_with_local_tree()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)

    with pytest.raises(FinalMaterializationError, match="exactly match"):
        _materialize_private_stage(
            plan,
            stage,
            canonical_wheel=canonical_wheel(),
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath(context_path), source, kind="file"
                ),
            ),
        )


def test_local_copy_publishes_bytes_independent_from_the_source(tmp_path: Path) -> None:
    source = tmp_path / "model.bin"
    original = b"abcdefgh"
    source.write_bytes(original)
    plan, context_path = _plan_with_local_file()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)

    _materialize_private_stage(
        plan,
        stage,
        canonical_wheel=canonical_wheel(),
        local_sources=(
            LocalMaterializationSource(
                PurePosixPath(context_path), source, kind="file"
            ),
        ),
        local_file_mode="copy",
    )
    source.write_bytes(b"changed")

    assert (stage / context_path).read_bytes() == original


@pytest.mark.parametrize(
    ("mode", "succeeds"),
    [("auto", True), ("clone", False)],
)
def test_clone_unavailable_falls_back_only_in_auto_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    succeeds: bool,
) -> None:
    source = tmp_path / "model.bin"
    content = b"local model"
    source.write_bytes(content)
    plan, context_path = _plan_with_local_file()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)

    def unavailable(
        _reader: file_admission.AdmittedRegularFileReader, _fd: int
    ) -> None:
        raise file_admission.FileCloneUnavailableError("clone unavailable")

    monkeypatch.setattr(
        file_admission.AdmittedRegularFileReader,
        "clone_to",
        unavailable,
    )

    def call() -> None:
        _materialize_private_stage(
            plan,
            stage,
            canonical_wheel=canonical_wheel(),
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath(context_path), source, kind="file"
                ),
            ),
            local_file_mode=mode,
        )

    if succeeds:
        call()
        assert (stage / context_path).read_bytes() == content
    else:
        with pytest.raises(FinalMaterializationError, match="clone is unavailable"):
            call()


def test_locked_local_materialization_rejects_acquired_content_drift(
    tmp_path: Path,
) -> None:
    intended = b"intended local model"
    source = tmp_path / "model.bin"
    source.write_bytes(b"changed local model")
    digest = f"sha256:{hashlib.sha256(intended).hexdigest()}"
    plan, context_path = _plan_with_local_file(digest=digest)
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)

    with pytest.raises(FinalMaterializationError, match="digest"):
        _materialize_private_stage(
            plan,
            stage,
            canonical_wheel=canonical_wheel(),
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath(context_path), source, kind="file"
                ),
            ),
            local_file_mode="copy",
        )


@pytest.mark.parametrize("locked", [False, True])
def test_tree_check_placeholders_are_projected_from_plan(
    tmp_path: Path,
    locked: bool,
) -> None:
    plan, context_path = _plan_with_local_tree(locked=locked)
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)

    _materialize_private_stage(
        plan,
        stage,
        canonical_wheel=canonical_wheel(),
        local_sources=(
            LocalMaterializationSource(
                PurePosixPath(context_path), tmp_path / "unused-source", kind="tree"
            ),
        ),
        check_placeholders=True,
    )

    context = stage / context_path
    assert (context / ".hidden" / "empty").is_dir()
    assert (context / ".hidden" / "payload.bin").read_bytes() == b""
    assert (context / "nested" / "payload.bin").read_bytes() == b""


@pytest.mark.parametrize("matches_plan", [True, False])
def test_locked_clone_verifies_acquired_bytes_instead_of_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matches_plan: bool,
) -> None:
    intended = b"AAAA"
    other = b"BBBB"
    source = tmp_path / "model.bin"
    source.write_bytes(other if matches_plan else intended)
    acquired = intended if matches_plan else other
    digest = f"sha256:{hashlib.sha256(intended).hexdigest()}"
    plan, context_path = _plan_with_local_file(digest=digest)
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)

    def clone_result(_reader, destination_fd):
        # Model the acquired snapshot independently from the later source view.
        assert os.write(destination_fd, acquired) == len(acquired)

    def reject_source_read(*_args, **_kwargs):
        pytest.fail("clone verification consumed source content")

    monkeypatch.setattr(
        file_admission.AdmittedRegularFileReader, "clone_to", clone_result
    )
    monkeypatch.setattr(
        file_admission.AdmittedRegularFileReader, "read_chunk", reject_source_read
    )
    monkeypatch.setattr(materializer_module, "_CLONE_VERIFY_CHUNK_BYTES", 2)

    def materialize():
        _materialize_private_stage(
            plan,
            stage,
            canonical_wheel=canonical_wheel(),
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath(context_path), source, kind="file"
                ),
            ),
            local_file_mode="clone",
        )

    if matches_plan:
        materialize()
    else:
        with pytest.raises(FinalMaterializationError, match="digest"):
            materialize()
    assert (stage / context_path).read_bytes() == acquired


def test_materializer_avoids_posix_mode_calls_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    output = tmp_path / "output"
    output.mkdir()

    monkeypatch.setattr(materializer_module, "_platform_name", "nt")
    monkeypatch.setattr(
        materializer_module.os,
        "fchmod",
        lambda *_args: pytest.fail("Windows materialization called fchmod"),
        raising=False,
    )
    monkeypatch.setattr(
        Path,
        "chmod",
        lambda *_args, **_kwargs: pytest.fail("Windows materialization called chmod"),
    )

    _materialize_private_stage(plan, output, canonical_wheel=canonical_wheel())

    assert (output / "build-plan.json").is_file()
    assert (output / "runtime/config.toml").is_file()


def _run_blocks(rendered: str) -> tuple[str, ...]:
    return tuple(f"RUN {block}" for block in rendered.split("\nRUN ")[1:])


def _flatten_command(block: str) -> str:
    lines = []
    for line in block.splitlines():
        line = line.strip()
        if line.endswith("\\"):
            line = line[:-1].rstrip()
        lines.append(line)
    return " ".join(lines)


# Materialization rechecks the retained wheel bytes before admitting them.
def test_materializer_rejects_canonical_wheel_byte_drift(tmp_path: Path) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    wheel = canonical_wheel()
    changed = CanonicalWheel(
        filename=wheel.filename,
        version=wheel.version,
        digest=wheel.digest,
        content=wheel.content + b"changed",
    )
    output = tmp_path / "output"
    output.mkdir(mode=0o700)

    with pytest.raises(FinalMaterializationError, match="does not match BuildPlan"):
        _materialize_private_stage(plan, output, canonical_wheel=changed)


# Private-stage admission rejects invalid entry shapes before writing any output.
@pytest.mark.parametrize("entry", ["missing", "file", "symlink", "nonempty"])
def test_materializer_rejects_invalid_private_stage_entry(
    tmp_path: Path,
    entry: str,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    stage = tmp_path / "stage"
    sentinel: Path | None = None
    real_stage: Path | None = None
    if entry == "file":
        stage.write_text("not a directory")
    elif entry == "symlink":
        real_stage = tmp_path / "real-stage"
        real_stage.mkdir(mode=0o700)
        stage.symlink_to(real_stage, target_is_directory=True)
    elif entry == "nonempty":
        stage.mkdir(mode=0o700)
        sentinel = stage / "sentinel"
        sentinel.write_text("keep")

    with pytest.raises(FinalMaterializationError, match="stage"):
        _materialize_private_stage(plan, stage, canonical_wheel=canonical_wheel())

    if entry == "missing":
        assert not stage.exists()
    elif entry == "file":
        assert stage.read_text() == "not a directory"
    elif entry == "symlink":
        assert real_stage is not None
        assert stage.is_symlink()
        assert stage.readlink().samefile(real_stage)
        assert tuple(real_stage.iterdir()) == ()
    else:
        assert sentinel is not None
        assert tuple(stage.iterdir()) == (sentinel,)
        assert sentinel.read_text() == "keep"


# The configured shutdown budget remains exact through planning and baked runtime copy.
def test_nondefault_shutdown_timeout_projects_to_plan_and_baked_runtime(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        final_config(shutdown_timeout=55.5),
        accepted_resolution(),
    )
    output = tmp_path / "output"
    output.mkdir(mode=0o700)

    _materialize_private_stage(plan, output, canonical_wheel=canonical_wheel())

    runtime = tomllib.loads((output / "runtime/config.toml").read_text())
    assert plan.runtime.shutdown_timeout == 55.5
    assert runtime["cdh"]["shutdown_timeout"] == 55.5


def test_normalized_host_ssh_key_set_is_written_once_to_baked_runtime(
    tmp_path: Path,
) -> None:
    document = final_config().model_dump(mode="json", exclude_none=True)
    document["system"]["ssh"]["pub_keys"] = [
        " ",
        f"  {_VALID_SSH_KEY}  ",
        _VALID_SSH_KEY.rsplit(" ", 1)[0] + " second@example",
    ]
    config = validate_final_config_structure(document)
    plan = build_plan(config, accepted_resolution())
    output = tmp_path / "output"
    output.mkdir(mode=0o700)

    _materialize_private_stage(plan, output, canonical_wheel=canonical_wheel())

    runtime = tomllib.loads((output / "runtime/config.toml").read_text())
    assert runtime["system"]["ssh"]["pub_keys"] == [_VALID_SSH_KEY]


def test_comfyui_root_file_is_materialized_and_reloaded_canonically(
    tmp_path: Path,
) -> None:
    document = final_config().model_dump(mode="json", exclude_none=True)
    document["files"][0]["target"] = "root.safetensors"
    config = validate_final_config_structure(document)
    plan = build_plan(config, accepted_resolution())
    output = tmp_path / "output"
    output.mkdir(mode=0o700)

    _materialize_private_stage(plan, output, canonical_wheel=canonical_wheel())

    baked_document = tomllib.loads((output / "runtime/config.toml").read_text())
    runtime = load_runtime_config(
        baked_config_path=output / "runtime/config.toml",
        mounted_config_path=tmp_path / "missing.toml",
        environ={},
    )
    assert baked_document["files"][0]["source"] == (
        "https://example.test/model.safetensors"
    )
    assert baked_document["files"][0]["target"] == "root.safetensors"
    assert runtime.files[0]["target"] == "root.safetensors"


@pytest.mark.skipif(
    os.name != "posix", reason="exercises the POSIX descriptor admission backend"
)
def test_file_admission_closes_the_leaf_and_preserves_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.json"
    source.write_bytes(b"{}")
    real_close = os.close
    closed: list[int] = []

    def close_then_report_error(descriptor: int) -> None:
        real_close(descriptor)
        closed.append(descriptor)
        raise OSError("close sentinel")

    def fail_admission(_descriptor: int) -> os.stat_result:
        raise OSError("admission sentinel")

    monkeypatch.setattr(file_admission, "_close_descriptor", close_then_report_error)
    monkeypatch.setattr(file_admission.os, "fstat", fail_admission)

    with pytest.raises(OSError, match="admission sentinel"):
        file_admission.read_regular_absolute_file(source)

    assert len(closed) == 1


@pytest.mark.skipif(
    os.name != "posix", reason="exercises the POSIX descriptor admission backend"
)
def test_file_admission_reports_local_close_error_inside_outer_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.json"
    source.write_bytes(b"{}")
    real_close = os.close
    closed: list[int] = []

    def close_then_report_first_error(descriptor: int) -> None:
        real_close(descriptor)
        closed.append(descriptor)
        if len(closed) == 1:
            raise OSError("close sentinel")

    monkeypatch.setattr(
        file_admission, "_close_descriptor", close_then_report_first_error
    )

    try:
        raise RuntimeError("outer sentinel")
    except RuntimeError:
        with pytest.raises(OSError, match="close sentinel"):
            file_admission.read_regular_absolute_file(source)

    assert len(closed) == 1


# Materialization accepts only exact locked hook identities and verified source bytes.
def test_materializer_direct_call_reuses_shared_runtime_hook_identity(
    tmp_path: Path,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    forged = plan.model_copy(
        update={
            "runtime": plan.runtime.model_copy(
                update={
                    "hooks": (
                        HookPlan.model_construct(
                            relative_path="pre-start.d/nested/hook.py",
                            digest=f"sha256:{'a' * 64}",
                        ),
                    )
                }
            )
        }
    )
    output = tmp_path / "output"
    output.mkdir(mode=0o700)

    with pytest.raises(FinalMaterializationError, match="hook identity is invalid"):
        _materialize_private_stage(forged, output, canonical_wheel=canonical_wheel())


def test_materializer_rejects_missing_extra_or_changed_local_sources(
    tmp_path: Path,
) -> None:
    content = b"hook"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    build_hooks = tmp_path / "build_hooks"
    source = build_hooks / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(build_hooks_dir=build_hooks, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    output = tmp_path / "output"
    output.mkdir(mode=0o700)

    with pytest.raises(FinalMaterializationError, match="exactly match"):
        _materialize_private_stage(plan, output, canonical_wheel=canonical_wheel())

    source.write_bytes(b"changed")
    with pytest.raises(FinalMaterializationError, match="digest"):
        _materialize_private_stage(
            plan,
            output,
            canonical_wheel=canonical_wheel(),
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath("build-hooks/hooks/pre.py"), source, kind="file"
                ),
            ),
        )


def test_materializer_writes_the_same_admitted_bytes_it_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admitted = b"admitted hook bytes\n"
    digest = f"sha256:{hashlib.sha256(admitted).hexdigest()}"
    build_hooks = tmp_path / "build_hooks"
    source = build_hooks / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"different on-disk bytes")
    plan = build_plan(
        final_config(build_hooks_dir=build_hooks, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    monkeypatch.setattr(
        materializer_module,
        "read_regular_absolute_file",
        lambda _path: admitted,
    )

    _materialize_private_stage(
        plan,
        output,
        canonical_wheel=canonical_wheel(),
        local_sources=(
            LocalMaterializationSource(
                PurePosixPath("build-hooks/hooks/pre.py"), source, kind="file"
            ),
        ),
    )

    assert (output / "build/hooks/hooks/pre.py").read_bytes() == admitted


# Strict parsing rejects forged path identities before materialization can trust them.
@pytest.mark.parametrize(
    "relative_path",
    [
        "../escape.py",
        "/tmp/escape.py",
        "hooks\\pre.py",
        "C:/escape.py",
        "C:escape.py",
        "",
        "hooks/./pre.py",
    ],
)
def test_parsed_build_plan_rejects_unsafe_hook_paths(
    tmp_path: Path,
    relative_path: str,
) -> None:
    content = b"hook"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    build_hooks = tmp_path / "build_hooks"
    source = build_hooks / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(build_hooks_dir=build_hooks, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    document = json.loads(dump_build_plan_json(plan))
    document["custom_nodes"]["nodes"][0]["pre_install_hooks"][0]["relative_path"] = (
        relative_path
    )

    with pytest.raises(ValidationError, match="canonical safe POSIX path"):
        parse_build_plan_json(json.dumps(document))

    assert not (tmp_path / "escape.py").exists()


# Source and destination traversal rejects symlinks and special filesystem nodes.
def test_materializer_rejects_symlink_source_and_symlink_parent(tmp_path: Path) -> None:
    content = b"hook"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    build_hooks = tmp_path / "build_hooks"
    source = build_hooks / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(build_hooks_dir=build_hooks, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )

    real_source = tmp_path / "real.py"
    real_source.write_bytes(content)
    source.unlink()
    source.symlink_to(real_source)
    output = tmp_path / "symlink-output"
    output.mkdir(mode=0o700)
    with pytest.raises(FinalMaterializationError, match="regular file"):
        _materialize_private_stage(
            plan,
            output,
            canonical_wheel=canonical_wheel(),
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath("build-hooks/hooks/pre.py"), source, kind="file"
                ),
            ),
        )

    source.unlink()
    source.write_bytes(content)
    source.chmod(0o755)
    real_build_hooks = tmp_path / "real-build-hooks"
    build_hooks.rename(real_build_hooks)
    build_hooks.symlink_to(real_build_hooks, target_is_directory=True)
    output = tmp_path / "parent-symlink-output"
    output.mkdir(mode=0o700)
    with pytest.raises(FinalMaterializationError, match="regular file"):
        _materialize_private_stage(
            plan,
            output,
            canonical_wheel=canonical_wheel(),
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath("build-hooks/hooks/pre.py"),
                    build_hooks / "hooks/pre.py",
                    kind="file",
                ),
            ),
        )


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX FIFO")
def test_materializer_rejects_special_source_file(tmp_path: Path) -> None:
    content = b"hook"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    build_hooks = tmp_path / "build_hooks"
    source = build_hooks / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(build_hooks_dir=build_hooks, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    source.unlink()
    os.mkfifo(source)
    output = tmp_path / "special-output"
    output.mkdir(mode=0o700)

    with pytest.raises(FinalMaterializationError, match="regular file"):
        _materialize_private_stage(
            plan,
            output,
            canonical_wheel=canonical_wheel(),
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath("build-hooks/hooks/pre.py"), source, kind="file"
                ),
            ),
        )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
