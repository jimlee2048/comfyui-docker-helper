"""Renderer, materializer, and phase-admission contracts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError
from tests.unit.test_build_plan import accepted_resolution, build_plan, final_config

from comfyui_docker_helper.application_checkers import APPLICATION_CHECKER_SOURCE
from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    HookPlan,
    build_plan_digest,
    dump_build_plan_json,
    parse_build_plan_json,
)
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.runtime_config import load_runtime_config
from comfyui_docker_helper.container import file_admission
from comfyui_docker_helper.container.build_plan_input import BuildPlanInputAdmission
from comfyui_docker_helper.rendering import final_materializer
from comfyui_docker_helper.rendering.final_materializer import (
    FinalMaterializationError,
    LocalMaterializationSource,
    materialize_build_plan,
)
from comfyui_docker_helper.rendering.final_renderer import (
    render_build_plan_dockerfile,
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
    assert "COPY --from=uv /uv /uvx /usr/local/bin/" in rendered
    assert "ARG " not in rendered
    assert "\nCMD " not in rendered
    assert "${PATH}" in rendered
    instructions = rendered.splitlines()
    assert [line for line in instructions if line.startswith("STOPSIGNAL ")] == [
        "STOPSIGNAL SIGTERM"
    ]
    assert [line for line in instructions if line.startswith("ENTRYPOINT ")] == [
        'ENTRYPOINT ["/opt/uv/bin/cdh", "container", "entrypoint"]'
    ]
    assert instructions[-2:] == [
        "STOPSIGNAL SIGTERM",
        'ENTRYPOINT ["/opt/uv/bin/cdh", "container", "entrypoint"]',
    ]
    assert rendered == render_build_plan_dockerfile(plan)


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
    custom_node_line = next(
        line for line in rendered.splitlines() if "install-custom-nodes" in line
    )
    assert custom_node_line.startswith("RUN /opt/uv/bin/cdh container")
    assert f"--build-plan-digest {build_plan_digest(plan)}" in custom_node_line
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


# Materialization writes one deterministic BuildPlan and verified local inputs.
def test_materializer_writes_deterministic_plan_and_verified_input(
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
    admission = BuildPlanInputAdmission.from_path(
        first / "build-plan.json",
        expected_build_plan_digest=expected,
    )
    assert admission.comfyui_install() == (plan.application, plan.toolchain)
    assert admission.custom_node_install() == (
        plan.custom_nodes,
        plan.application,
    )
    assert admission.file_downloads() == (
        plan.files,
        plan.application.paths.comfyui,
    )


# Canonical plan bytes bound to the Dockerfile literal authorize each installer input.
def test_build_plan_admission_rejects_changed_plan_under_literal_digest(
    tmp_path: Path,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    output = tmp_path / "output"
    output.mkdir()
    materialize_build_plan(plan, output)
    document = json.loads((output / "build-plan.json").read_bytes())
    document["runtime"]["environment"][0]["value"] = "changed"
    (output / "build-plan.json").write_text(json.dumps(document))

    with pytest.raises(ValueError, match="expected digest"):
        BuildPlanInputAdmission.from_path(
            output / "build-plan.json",
            expected_build_plan_digest=build_plan_digest(plan),
        )


# The configured shutdown budget remains exact through planning and baked runtime copy.
def test_nondefault_shutdown_timeout_projects_to_plan_and_baked_runtime(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        final_config(shutdown_timeout=55.5),
        accepted_resolution(),
    )
    output = tmp_path / "output"
    output.mkdir()

    materialize_build_plan(plan, output)

    runtime = tomllib.loads((output / "runtime/config.toml").read_text())
    assert plan.runtime.shutdown_timeout == 55.5
    assert runtime["cdh"]["shutdown_timeout"] == 55.5


# Descriptor-relative BuildPlan admission rejects substituted inputs.
def test_build_plan_admission_rejects_leaf_and_ancestor_symlinks(
    tmp_path: Path,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    context = tmp_path / "context"
    context.mkdir()
    materialize_build_plan(plan, context)
    digest = build_plan_digest(plan)

    leaf_link = tmp_path / "build-plan-link.json"
    leaf_link.symlink_to(context / "build-plan.json")
    with pytest.raises(ValueError, match="could not read canonical BuildPlan"):
        BuildPlanInputAdmission.from_path(
            leaf_link,
            expected_build_plan_digest=digest,
        )

    ancestor_link = tmp_path / "context-link"
    ancestor_link.symlink_to(context, target_is_directory=True)
    with pytest.raises(ValueError, match="could not read canonical BuildPlan"):
        BuildPlanInputAdmission.from_path(
            ancestor_link / "build-plan.json",
            expected_build_plan_digest=digest,
        )


def test_build_plan_admission_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "build-plan.fifo"
    os.mkfifo(fifo)
    script = """
import sys
from comfyui_docker_helper.container.build_plan_input import BuildPlanInputAdmission

try:
    BuildPlanInputAdmission.from_path(
        sys.argv[1], expected_build_plan_digest="sha256:" + "a" * 64
    )
except ValueError as error:
    assert str(error) == "could not read canonical BuildPlan"
else:
    raise AssertionError("FIFO was admitted")
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(fifo)],
        check=False,
        capture_output=True,
        timeout=3,
    )

    assert result.returncode == 0, result.stderr.decode()


def test_file_admission_attempts_every_close_and_preserves_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
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

    with pytest.raises(OSError, match="materialized input must be a regular file"):
        file_admission.read_regular_absolute_file(fifo)

    assert len(closed) >= 3
    assert len(closed) == len(set(closed))


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

    assert len(closed) >= 3
    assert len(closed) == len(set(closed))


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
    output.mkdir()

    with pytest.raises(FinalMaterializationError, match="hook identity is invalid"):
        materialize_build_plan(forged, output)

    assert tuple(output.iterdir()) == ()


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


# Source and destination traversal rejects symlinks and special filesystem nodes.
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
