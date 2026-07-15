"""Focused renderer, materializer, and phase-loader contracts."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError
from tests.unit.test_build_plan import accepted_resolution, build_plan, final_config

from comfyui_docker_helper.application_checkers import APPLICATION_CHECKER_SOURCE
from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    build_plan_digest,
    dump_build_plan_json,
    parse_build_plan_json,
)
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.runtime_config import load_runtime_config
from comfyui_docker_helper.container.phase_inputs import load_phase_input
from comfyui_docker_helper.rendering import final_materializer
from comfyui_docker_helper.rendering.final_materializer import (
    FinalMaterializationError,
    LocalMaterializationSource,
    materialize_build_plan,
)
from comfyui_docker_helper.rendering.final_renderer import (
    render_build_plan_dockerfile,
)


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
    assert "COPY --from=uv /uv /uvx /usr/local/bin/" in rendered
    assert "ARG " not in rendered
    assert "${PATH}" in rendered
    assert rendered == render_build_plan_dockerfile(plan)


def test_renderer_quotes_container_paths_without_host_projection() -> None:
    config = final_config()
    document = config.model_dump(mode="python")
    document["system"]["workspace"] = "/workspace data"
    changed = FinalConfig.model_validate(document)

    rendered = render_build_plan_dockerfile(build_plan(changed, accepted_resolution()))

    assert 'ENV WORKSPACE="/workspace data"' in rendered
    assert 'WORKDIR "/workspace data"' in rendered


def test_renderer_preserves_non_package_runtime_environment() -> None:
    rendered = render_build_plan_dockerfile(
        build_plan(final_config(), accepted_resolution())
    )

    assert 'ENV ALPHA="first"' in rendered
    assert 'ENV ZED="last"' in rendered


def test_renderer_installs_isolated_comfy_cli_before_generic_tools() -> None:
    plan = build_plan(
        final_config(with_uv_tool=True), accepted_resolution(with_uv_tool=True)
    )

    rendered = render_build_plan_dockerfile(plan)

    cdh_install = rendered.index("/opt/cdh/wheel/*.whl")
    cli_install = rendered.index("comfy-cli==1.8.0")
    generic_install = rendered.index("ruff==0.15.18")
    application_install = rendered.index("container install-comfyui")
    assert cdh_install < cli_install < generic_install < application_install
    cdh_block = rendered[cdh_install:cli_install]
    assert (
        "uv --no-config pip check --python "
        "/opt/uv/tools/comfyui-docker-helper/bin/python --no-python-downloads"
        in cdh_block
    )
    assert "--force" not in rendered
    cli_block = rendered[cli_install:generic_install]
    assert "--with" not in cli_block
    assert "UV_CONSTRAINT" not in cli_block
    assert "PIP_CONSTRAINT" not in cli_block
    assert "/opt/venv" not in cli_block
    assert (
        "uv --no-config pip check --python /opt/uv/tools/comfy-cli/bin/python"
        in cli_block
    )
    assert "/opt/cdh/build/comfy-cli-inventory.txt" in cli_block
    assert "sys._base_executable" in cli_block
    assert "console_scripts" in cli_block
    for command in ("comfy", "comfy-cli", "comfycli"):
        assert f"/opt/uv/bin/{command}" in rendered
        assert f"/opt/uv/tools/comfy-cli/bin/{command}" in rendered
    assert " --help" not in rendered
    assert 'UV_TOOL_DIR="/opt/uv/tools"' in rendered
    assert 'UV_TOOL_BIN_DIR="/opt/uv/bin"' in rendered
    assert 'ENV PATH="/opt/uv/bin:/opt/venv/bin:$' + '{PATH}"' in rendered
    assert plan.runtime.launch_command[0] == "/opt/venv/bin/python"
    assert (
        "uv --no-config pip check --python /opt/uv/tools/ruff/bin/python "
        "--no-python-downloads" in rendered
    )


def test_renderer_disabled_mode_reserves_no_comfy_cli_commands() -> None:
    rendered = render_build_plan_dockerfile(
        build_plan(
            final_config(install_cli=False), accepted_resolution(install_cli=False)
        )
    )

    assert "uv --no-config tool install" in rendered  # cdh remains a uv tool.
    assert "comfy-cli==" not in rendered
    assert "comfy-cli-inventory.txt" not in rendered
    for command in ("comfy", "comfy-cli", "comfycli"):
        assert f"test ! -e /opt/uv/bin/{command}" in rendered
        assert f"test ! -L /opt/uv/bin/{command}" in rendered


def test_renderer_runs_complete_custom_node_sequence_in_one_later_layer() -> None:
    plan = build_plan(
        final_config(install_cli=False), accepted_resolution(install_cli=False)
    )

    rendered = render_build_plan_dockerfile(plan)

    assert rendered.count("container install-custom-nodes") == 1
    assert rendered.index("container install-comfyui") < rendered.index(
        "container install-custom-nodes"
    )
    custom_node_line = next(
        line for line in rendered.splitlines() if "install-custom-nodes" in line
    )
    assert custom_node_line.startswith("RUN /opt/uv/bin/cdh container")
    assert "--custom-nodes-phase /opt/cdh/build/phases/custom-nodes.json" in (
        custom_node_line
    )
    assert "--application-phase /opt/cdh/build/phases/application.json" in (
        custom_node_line
    )
    assert "--constraints /opt/cdh/build/python-package-constraints.txt" in (
        custom_node_line
    )
    assert "--hooks-directory /opt/cdh/build/inputs" in custom_node_line
    assert "comfy node" not in rendered
    assert "comfy install" not in rendered


@pytest.mark.parametrize("node_type", ["git", "registry", "empty"])
def test_renderer_always_emits_one_custom_node_layer(node_type: str) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["custom_nodes"]["nodes"] = tuple(
        node
        for node in document["custom_nodes"]["nodes"]
        if node_type != "empty" and node["type"] == node_type
    )
    changed = BuildPlan.model_validate(document)

    rendered = render_build_plan_dockerfile(changed)

    assert rendered.count("container install-custom-nodes") == 1


@pytest.mark.parametrize(
    ("install_cli", "install_manager", "node_types"),
    [
        (True, True, ("registry",)),
        (False, True, ("registry",)),
        (True, True, ("git",)),
        (False, False, ("git",)),
        (True, True, ("registry", "git")),
        (False, True, ("git", "registry")),
        (True, False, ()),
    ],
)
def test_final_application_mode_matrix_keeps_one_observed_execution_boundary(
    install_cli: bool,
    install_manager: bool,
    node_types: tuple[str, ...],
) -> None:
    plan = build_plan(
        final_config(install_cli=install_cli),
        accepted_resolution(install_cli=install_cli),
    )
    document = plan.model_dump(mode="python")
    available = {node["type"]: node for node in document["custom_nodes"]["nodes"]}
    document["custom_nodes"]["nodes"] = tuple(available[item] for item in node_types)
    document["custom_nodes"]["install_manager"] = install_manager
    if not install_manager:
        document["application"]["comfyui"]["manager"] = None
    changed = BuildPlan.model_validate(document)

    rendered = render_build_plan_dockerfile(changed)

    assert rendered.count("container install-custom-nodes") == 1
    assert tuple(node.type for node in changed.custom_nodes.nodes) == node_types
    assert changed.application.inventory_path == (
        "/opt/cdh/build/application-inventory.txt"
    )
    assert (changed.toolchain.tool_store.comfy_cli is not None) is install_cli
    assert (changed.application.comfyui.manager is not None) is install_manager


def test_materializer_writes_deterministic_plan_phases_and_verified_input(
    tmp_path: Path,
) -> None:
    content = b"#!/usr/bin/env python3\n"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    scripts = tmp_path / "scripts"
    source = scripts / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(scripts_dir=scripts, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    source_input = LocalMaterializationSource(
        PurePosixPath("custom-node-hooks/hooks/pre.py"), source
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    materialize_build_plan(plan, first, local_sources=(source_input,))
    materialize_build_plan(plan, second, local_sources=(source_input,))

    assert (first / "build-plan.json").read_bytes() == dump_build_plan_json(plan)
    assert (first / "inputs/hooks/pre.py").read_bytes() == content
    assert (first / "Dockerfile").read_text() == render_build_plan_dockerfile(plan)
    assert (
        "COPY runtime/config.toml /opt/cdh/runtime/config.toml"
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
    assert tree["checkers/application.py"] == APPLICATION_CHECKER_SOURCE.read_bytes()
    assert "cdh/pyproject.toml" in tree
    assert "cdh/src/comfyui_docker_helper/cli.py" in tree
    assert "cdh-production-requirements.txt" in tree
    assert "cdh-production-inventory.txt" in tree
    routing = tree["pytorch-resolution.toml"].decode()
    routing_document = tomllib.loads(routing)
    source_map = routing_document["tool"]["uv"]["sources"]
    expected_source_packages = {
        package.name for package in plan.application.pytorch.packages
    }
    assert set(source_map) == expected_source_packages
    assert "torchaudio" in source_map
    assert all(source == {"index": "pytorch"} for source in source_map.values())
    assert 'url = "https://pypi.org/simple"' in routing
    assert 'url = "https://download.pytorch.org/whl/cu130"' in routing
    assert '[tool.uv.sources.torch]\nindex = "pytorch"' in routing
    assert '[tool.uv.sources.torchvision]\nindex = "pytorch"' in routing
    assert not (first / "config.toml").exists()
    assert not (first / "config.lock.toml").exists()
    assert str(source).encode() not in (first / "build-plan.json").read_bytes()

    dockerfile = (first / "Dockerfile").read_text()
    assert (
        "COPY --chown=0:0 --chmod=0444 pytorch-resolution.toml "
        "/opt/cdh/build/pyproject.toml" in dockerfile
    )
    assert "container install-comfyui" in dockerfile
    assert "COPY --chown=0:0 checkers /opt/cdh/build/checkers" in dockerfile
    assert "/opt/cdh/build/checkers/application.py inventory" in dockerfile
    assert "torch==2.12.1+cu130" not in dockerfile
    assert "UV_CONSTRAINT" not in dockerfile
    assert "PIP_CONSTRAINT" not in dockerfile

    expected = build_plan_digest(plan)
    assert (
        load_phase_input(
            first / "phases/build.json",
            "build",
            expected_build_plan_digest=expected,
        )
        == plan.build
    )
    assert (
        load_phase_input(
            first / "phases/toolchain.json",
            "toolchain",
            expected_build_plan_digest=expected,
        )
        == plan.toolchain
    )
    assert (
        load_phase_input(
            first / "phases/application.json",
            "application",
            expected_build_plan_digest=expected,
        )
        == plan.application
    )
    assert (
        load_phase_input(
            first / "phases/custom-nodes.json",
            "custom-nodes",
            expected_build_plan_digest=expected,
        )
        == plan.custom_nodes
    )
    assert (
        load_phase_input(
            first / "phases/files.json",
            "files",
            expected_build_plan_digest=expected,
        )
        == plan.files
    )
    assert (
        load_phase_input(
            first / "phases/runtime.json",
            "runtime",
            expected_build_plan_digest=expected,
        )
        == plan.runtime
    )


def test_phase_loader_rejects_wrong_binding_wrong_phase_and_extra_fields(
    tmp_path: Path,
) -> None:
    content = b"#!/bin/sh\n"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    scripts = tmp_path / "scripts"
    source = scripts / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(scripts_dir=scripts, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    output = tmp_path / "output"
    output.mkdir()
    materialize_build_plan(
        plan,
        output,
        local_sources=(
            LocalMaterializationSource(
                PurePosixPath("custom-node-hooks/hooks/pre.py"), source
            ),
        ),
    )
    phase_path = output / "phases/toolchain.json"

    with pytest.raises(ValueError, match="different BuildPlan"):
        load_phase_input(
            phase_path,
            "toolchain",
            expected_build_plan_digest=f"sha256:{'0' * 64}",
        )
    with pytest.raises(ValidationError):
        load_phase_input(
            phase_path,
            "files",
            expected_build_plan_digest=build_plan_digest(plan),
        )

    document = json.loads(phase_path.read_text())
    document["unknown"] = True
    phase_path.write_text(json.dumps(document))
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_phase_input(
            phase_path,
            "toolchain",
            expected_build_plan_digest=build_plan_digest(plan),
        )


def test_materializer_rejects_missing_extra_or_changed_local_sources(
    tmp_path: Path,
) -> None:
    content = b"hook"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    scripts = tmp_path / "scripts"
    source = scripts / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(scripts_dir=scripts, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(FinalMaterializationError, match="exactly match"):
        materialize_build_plan(plan, output)
    assert tuple(output.iterdir()) == ()

    source.write_bytes(b"changed")
    with pytest.raises(FinalMaterializationError, match="digest"):
        materialize_build_plan(
            plan,
            output,
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath("custom-node-hooks/hooks/pre.py"), source
                ),
            ),
        )
    assert tuple(output.iterdir()) == ()


@pytest.mark.parametrize(
    "relative_path",
    ["../escape.py", "/tmp/escape.py", "hooks\\pre.py", "", "hooks/./pre.py"],
)
def test_parsed_build_plan_rejects_unsafe_hook_paths(
    tmp_path: Path,
    relative_path: str,
) -> None:
    content = b"hook"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    scripts = tmp_path / "scripts"
    source = scripts / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(scripts_dir=scripts, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    document = json.loads(dump_build_plan_json(plan))
    document["custom_nodes"]["nodes"][0]["pre_install"][0]["relative_path"] = (
        relative_path
    )

    with pytest.raises(ValidationError, match="canonical safe POSIX path"):
        parse_build_plan_json(json.dumps(document))

    assert not (tmp_path / "escape.py").exists()


def test_materializer_rejects_symlink_source_and_symlink_parent(tmp_path: Path) -> None:
    content = b"hook"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    scripts = tmp_path / "scripts"
    source = scripts / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(scripts_dir=scripts, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )

    real_source = tmp_path / "real.py"
    real_source.write_bytes(content)
    source.unlink()
    source.symlink_to(real_source)
    output = tmp_path / "symlink-output"
    output.mkdir()
    with pytest.raises(FinalMaterializationError, match="regular file"):
        materialize_build_plan(
            plan,
            output,
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath("custom-node-hooks/hooks/pre.py"), source
                ),
            ),
        )
    assert tuple(output.iterdir()) == ()

    source.unlink()
    source.write_bytes(content)
    source.chmod(0o755)
    real_scripts = tmp_path / "real-scripts"
    scripts.rename(real_scripts)
    scripts.symlink_to(real_scripts, target_is_directory=True)
    output = tmp_path / "parent-symlink-output"
    output.mkdir()
    with pytest.raises(FinalMaterializationError, match="source parent"):
        materialize_build_plan(
            plan,
            output,
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath("custom-node-hooks/hooks/pre.py"),
                    scripts / "hooks/pre.py",
                ),
            ),
        )
    assert tuple(output.iterdir()) == ()


def test_materializer_rejects_special_source_file(tmp_path: Path) -> None:
    content = b"hook"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    scripts = tmp_path / "scripts"
    source = scripts / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(scripts_dir=scripts, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    source.unlink()
    os.mkfifo(source)
    output = tmp_path / "special-output"
    output.mkdir()

    with pytest.raises(FinalMaterializationError, match="regular file"):
        materialize_build_plan(
            plan,
            output,
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath("custom-node-hooks/hooks/pre.py"), source
                ),
            ),
        )

    assert tuple(output.iterdir()) == ()


@pytest.mark.parametrize(
    "injected_type",
    ["parent-symlink", "parent-special", "final-symlink", "final-special"],
)
def test_materializer_rejects_symlink_or_special_destination_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injected_type: str,
) -> None:
    content = b"hook"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    scripts = tmp_path / "scripts"
    source = scripts / "hooks/pre.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    source.chmod(0o755)
    plan = build_plan(
        final_config(scripts_dir=scripts, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged")

    def inject_destination(_path: Path, _digest: str) -> bytes:
        if injected_type == "parent-symlink":
            (output / "inputs").symlink_to(outside, target_is_directory=True)
        elif injected_type == "parent-special":
            os.mkfifo(output / "inputs")
        else:
            parent = output / "inputs/hooks"
            parent.mkdir(parents=True)
            final = parent / "pre.py"
            if injected_type == "final-symlink":
                final.symlink_to(sentinel)
            else:
                os.mkfifo(final)
        return content

    monkeypatch.setattr(final_materializer, "_verified_source", inject_destination)

    with pytest.raises(FinalMaterializationError, match="symlink or special"):
        materialize_build_plan(
            plan,
            output,
            local_sources=(
                LocalMaterializationSource(
                    PurePosixPath("custom-node-hooks/hooks/pre.py"), source
                ),
            ),
        )

    assert tuple(output.iterdir()) == ()
    assert sentinel.read_text() == "unchanged"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
