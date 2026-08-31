"""Host local-source admission bundle behavior."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from comfyui_docker_helper.config.authored.service import load_validate_config_result
from comfyui_docker_helper.config.planning.request import LocalSourceRequest
from comfyui_docker_helper.host.context.local_inputs import (
    LocalFilePlanningInput,
    LocalInputAdmissionError,
    LocalTreePlanningInput,
    admit_local_inputs,
)


def _config_with_local(source: str, target: str, *, locked: bool = False) -> str:
    return f'''
[compute_platform]
type = "cuda"
[compute_platform.cuda]
version = "13.0.3"
image_flavor = "cudnn-devel"
image_distro = "ubuntu24.04"
[python]
version = "3.13.14"
uv_version = "0.11.28"
[pytorch]
version = "2.12.1"
[comfyui]
version = "0.11.0"
install_cli = true
install_manager = false
[build]
tags = ["example:test"]
platforms = ["linux/amd64"]

[[files]]
type = "local"
source = "{source}"
target = "{target}"
content_lock = {str(locked).lower()}
'''


def _request(target: str, *, locked: bool = False) -> LocalSourceRequest:
    return LocalSourceRequest(
        type="local",
        target=f"/opt/ComfyUI/{target}",
        relative_target=target,
        content_lock=locked,
    )


def test_tree_bundle_keeps_one_inventory_and_emits_one_empty_warning(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(_config_with_local("tree", "user/default/workflows"))
    result = load_validate_config_result(config)

    bundle = admit_local_inputs(
        result,
        (_request("user/default/workflows"),),
        tmp_path / "context",
    )

    assert len(bundle.planning_inputs) == 1
    planned = bundle.planning_inputs[0]
    assert isinstance(planned, LocalTreePlanningInput)
    assert planned.inventory.members == ()
    assert planned.tree_digest is None
    assert len(bundle.materialization_sources) == 1
    assert bundle.materialization_sources[0].source_path == source.absolute()
    assert len(bundle.warnings) == 1
    assert bundle.warnings[0].path == ("files", 0, "source")
    assert bundle.warnings[0].message == (
        "local source directory is empty; its target directory will still be "
        "present in the image"
    )


def test_locked_tree_bundle_contains_only_process_local_planning_facts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    (source / "payload").write_bytes(b"payload")
    config = tmp_path / "config.toml"
    config.write_text(_config_with_local("tree", "workflows", locked=True))
    result = load_validate_config_result(config)

    bundle = admit_local_inputs(
        result, (_request("workflows", locked=True),), tmp_path / "context"
    )
    planned = bundle.planning_inputs[0]
    assert isinstance(planned, LocalTreePlanningInput)
    assert planned.tree_digest is not None
    assert planned.tree_digest.startswith("sha256:")
    assert all(
        not hasattr(planned, field)
        for field in ("source", "source_path", "host_locator")
    )


def test_file_bundle_keeps_file_shape_and_source_output_separation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"model")
    config = tmp_path / "config.toml"
    config.write_text(
        _config_with_local(source.as_posix(), "models/model.bin", locked=True)
    )
    result = load_validate_config_result(config)

    bundle = admit_local_inputs(
        result,
        (_request("models/model.bin", locked=True),),
        tmp_path / "context",
    )
    planned = bundle.planning_inputs[0]
    assert isinstance(planned, LocalFilePlanningInput)
    assert planned.digest is not None

    with pytest.raises(LocalInputAdmissionError) as raised:
        admit_local_inputs(
            result, (_request("models/model.bin", locked=True),), source.parent
        )
    assert raised.value.diagnostics[0].code == "render.input_output_overlap"
    assert str(source) not in raised.value.diagnostics[0].message


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX symlink loop")
def test_self_loop_source_reports_content_safe_inspection_failure(
    tmp_path: Path,
) -> None:
    source_locator = "self-loop"
    source = tmp_path / source_locator
    source.symlink_to(source.name)
    config = tmp_path / "config.toml"
    config.write_text(_config_with_local(source_locator, "models/model.bin"))
    result = load_validate_config_result(config)

    with pytest.raises(LocalInputAdmissionError) as raised:
        admit_local_inputs(
            result,
            (_request("models/model.bin"),),
            tmp_path / "context",
        )

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "render.input_output_inspect_failed"
    assert source_locator not in diagnostic.message
    assert source_locator not in str(raised.value)


def test_reserved_tree_member_diagnostic_is_source_relative(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    (source / ".cdh-staging" / "payload").mkdir(parents=True)
    config = tmp_path / "config.toml"
    config.write_text(_config_with_local("tree", "workflows"))
    result = load_validate_config_result(config)

    with pytest.raises(LocalInputAdmissionError) as raised:
        admit_local_inputs(result, (_request("workflows"),), tmp_path / "context")
    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.path == ("files", 0, "source")
    assert diagnostic.code == "file.reserved_source_component"
    assert ".cdh-staging" in diagnostic.message
    assert str(source) not in diagnostic.message


def test_whiteout_tree_member_diagnostic_is_source_relative(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / ".wh.payload").write_bytes(b"whiteout")
    config = tmp_path / "config.toml"
    config.write_text(_config_with_local("tree", "workflows"))
    result = load_validate_config_result(config)

    with pytest.raises(LocalInputAdmissionError) as raised:
        admit_local_inputs(result, (_request("workflows"),), tmp_path / "context")

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.path == ("files", 0, "source")
    assert diagnostic.code == "file.reserved_source_component"
    assert "nested/.wh.payload" in diagnostic.message
    assert "deletion markers" in diagnostic.message
    assert "rename" in diagnostic.message
    assert str(source) not in diagnostic.message


def test_unmapped_whiteout_source_roots_are_admitted(
    tmp_path: Path,
) -> None:
    file_source = tmp_path / ".wh.model"
    file_source.write_bytes(b"model")
    file_config = tmp_path / "file.toml"
    file_config.write_text(_config_with_local(".wh.model", "models/model.bin"))
    file_result = load_validate_config_result(file_config)
    file_bundle = admit_local_inputs(
        file_result,
        (_request("models/model.bin"),),
        tmp_path / "file-context",
    )
    assert isinstance(file_bundle.planning_inputs[0], LocalFilePlanningInput)

    tree_source = tmp_path / ".wh.inputs"
    tree_source.mkdir()
    (tree_source / "normal.txt").write_bytes(b"normal")
    tree_config = tmp_path / "tree.toml"
    tree_config.write_text(_config_with_local(".wh.inputs", "workflows"))
    tree_result = load_validate_config_result(tree_config)
    tree_bundle = admit_local_inputs(
        tree_result,
        (_request("workflows"),),
        tmp_path / "tree-context",
    )
    planned = tree_bundle.planning_inputs[0]
    assert isinstance(planned, LocalTreePlanningInput)
    member_paths = [
        member.relative_path.as_posix() for member in planned.inventory.members
    ]
    assert member_paths == ["normal.txt"]


@pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX support for newline filenames"
)
def test_unsafe_whiteout_member_diagnostic_does_not_render_raw_name(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    unsafe_name = ".wh.\n"
    (source / unsafe_name).write_bytes(b"unsafe")
    config = tmp_path / "config.toml"
    config.write_text(_config_with_local("tree", "workflows"))
    result = load_validate_config_result(config)

    with pytest.raises(LocalInputAdmissionError) as raised:
        admit_local_inputs(result, (_request("workflows"),), tmp_path / "context")

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "file.invalid_source_member"
    assert unsafe_name not in diagnostic.message
    assert str(source) not in diagnostic.message
