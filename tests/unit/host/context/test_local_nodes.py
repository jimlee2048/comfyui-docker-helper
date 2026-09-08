"""Selected local-node inputs use the native SDK policy and safe admission."""

import os
import re
from pathlib import Path, PurePosixPath

import pytest
from docker.utils.build import PatternMatcher
from tests.unit.host.context.test_local_inputs import _config_with_local

from comfyui_docker_helper.config.authored.service import load_validate_config_result
from comfyui_docker_helper.config.planning.request import LocalNodeRequest
from comfyui_docker_helper.filesystem import admission
from comfyui_docker_helper.host.context import local_nodes
from comfyui_docker_helper.host.context.local_inputs import LocalInputAdmissionError
from comfyui_docker_helper.host.context.local_nodes import (
    DockerIgnoreSelection,
    admit_local_node_inputs,
    read_node_selection,
)


def _bundle(tmp_path: Path, *, locked: bool = False, output: Path | None = None):
    config = tmp_path / "config.toml"
    base = _config_with_local("node", "unused").split("[[files]]")[0]
    config.write_text(
        base
        + f"""\n[[comfyui.custom_nodes]]
type = "local"
source = "node"
target_dir = "example"
content_lock = {str(locked).lower()}
"""
    )
    result = load_validate_config_result(config)
    request = LocalNodeRequest(
        "local", "/opt/ComfyUI/custom_nodes/example", "example", locked, (), ()
    )
    return admit_local_node_inputs(result, (request,), output or tmp_path / "context")


@pytest.mark.parametrize(
    "rules",
    [b"cache\n", b"cache\n!cache/keep.py\n", b"*\n!*/keep.py\n", b"CACHE\n"],
    ids=["exclude", "literal-negation", "wildcard-negation", "native-case"],
)
def test_selected_inventory_matches_sdk_native_walk_with_ancestor_skeleton(
    tmp_path: Path, rules: bytes
):
    for name in ("cache/keep.py", "cache/drop.py", "nested/.dockerignore", "main.py"):
        member = tmp_path / name
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_bytes(b"code")
    (tmp_path / ".dockerignore").write_bytes(rules)
    selection = read_node_selection(tmp_path)
    actual = admission.admit_local_tree(tmp_path, selection=selection)
    expected = {
        PurePosixPath(Path(name).as_posix())
        for name in PatternMatcher(rules.decode().splitlines()).walk(str(tmp_path))
    }
    for name in tuple(expected):
        expected.update(
            parent for parent in name.parents if parent != PurePosixPath(".")
        )
    assert {item.relative_path for item in actual.members} == expected


def test_excluded_subtree_is_never_opened_or_admitted(tmp_path: Path, monkeypatch):
    source = tmp_path / "node"
    (source / ".venv").mkdir(parents=True)
    (source / ".venv" / ".wh.invalid").write_bytes(b"ignored")
    (source / ".dockerignore").write_bytes(b".venv\n")
    original = admission.os.scandir

    def scan(path):
        assert Path(path) != source / ".venv"
        return original(path)

    monkeypatch.setattr(admission.os, "scandir", scan)
    bundle = _bundle(tmp_path, locked=True)
    assert [
        m.relative_path.as_posix() for m in bundle.planning_inputs[0].inventory.members
    ] == [".dockerignore"]


@pytest.mark.skipif(os.name != "posix", reason="requires native symlink creation")
@pytest.mark.parametrize(
    "rules", [b"", b"link\n!link/child\n"], ids=["selected", "negated-descendant"]
)
def test_selected_or_traversal_link_fails_without_following(
    tmp_path: Path, rules: bytes
):
    source = tmp_path / "node"
    source.mkdir()
    (source / "link").symlink_to(tmp_path, target_is_directory=True)
    (source / ".dockerignore").write_bytes(rules)
    with pytest.raises(LocalInputAdmissionError) as raised:
        _bundle(tmp_path)
    assert raised.value.diagnostics[0].path == ("comfyui", "custom_nodes", 0, "source")
    assert str(source) not in str(raised.value)


def test_unlocked_admission_reads_control_only_and_freezes_policy(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "node"
    source.mkdir()
    rules = b"\xef\xbb\xbf# comment\r\nignored\r\n"
    (source / ".dockerignore").write_bytes(rules)
    (source / "main.py").write_bytes(b"code")
    original = admission.operate_regular_absolute_file

    def operate(path, operation):
        if Path(path).name != ".dockerignore":

            def metadata(reader):
                def forbidden(_limit=None):
                    pytest.fail("unlocked planning consumed source bytes")

                return operation(
                    admission.AdmittedRegularFileReader(
                        reader.size, reader.mode, forbidden
                    )
                )

            return original(path, metadata)
        return original(path, operation)

    monkeypatch.setattr(admission, "operate_regular_absolute_file", operate)
    bundle = _bundle(tmp_path)
    material = bundle.materialization_sources[0]
    assert material.control_file_bytes == rules
    assert material.selection is not None
    (source / ".dockerignore").write_bytes(b"main.py\n")
    assert material.selection.includes("main.py")
    assert bundle.planning_inputs[0].tree_digest is None


def test_locked_identity_ignores_excluded_bytes_but_binds_rule_comments(tmp_path: Path):
    source = tmp_path / "node"
    source.mkdir()
    (source / ".dockerignore").write_bytes(b"ignored\n")
    (source / "ignored").write_bytes(b"one")
    first = _bundle(tmp_path, locked=True).planning_inputs
    (source / "ignored").write_bytes(b"two")
    assert _bundle(tmp_path, locked=True).planning_inputs == first
    (source / ".dockerignore").write_bytes(b"# comment\nignored\n")
    assert _bundle(tmp_path, locked=True).planning_inputs != first


def test_empty_warning_and_control_file_retention(tmp_path: Path):
    source = tmp_path / "node"
    source.mkdir()
    assert len(_bundle(tmp_path).warnings) == 1
    (source / ".dockerignore").write_bytes(b"*\n")
    bundle = _bundle(tmp_path)
    assert not bundle.warnings
    assert bundle.planning_inputs[0].inventory.members[
        0
    ].relative_path == PurePosixPath(".dockerignore")


def test_node_source_cannot_contain_context_output(tmp_path: Path):
    source = tmp_path / "node"
    source.mkdir()
    (source / "main.py").write_bytes(b"code")
    with pytest.raises(LocalInputAdmissionError):
        _bundle(tmp_path, output=source / "context")


@pytest.mark.parametrize("data", [b"", b"# only comment\n", b"\xef\xbb\xbf\r\n"])
def test_empty_rules_preserve_all_members(data: bytes):
    selection = DockerIgnoreSelection(data)
    assert selection.includes(os.path.join("nested", "source.py"))
    assert selection.descends("nested")


def test_invalid_root_ignore_reports_safe_source_diagnostic(tmp_path: Path):
    source = tmp_path / "node"
    source.mkdir()
    (source / ".dockerignore").write_bytes(b"\xffprivate rule")
    with pytest.raises(LocalInputAdmissionError) as raised:
        _bundle(tmp_path)
    assert "private" not in str(raised.value)
    assert str(source) not in str(raised.value)


@pytest.mark.skipif(os.name != "posix", reason="requires native symlink creation")
def test_completely_excluded_link_is_ignored(tmp_path: Path):
    source = tmp_path / "node"
    source.mkdir()
    (source / "link").symlink_to(tmp_path, target_is_directory=True)
    (source / ".dockerignore").write_bytes(b"link\n")
    assert len(_bundle(tmp_path).planning_inputs[0].inventory.members) == 1


def test_sdk_pattern_error_is_a_controlled_diagnostic(tmp_path: Path, monkeypatch):
    source = tmp_path / "node"
    source.mkdir()

    def broken(_self, _path):
        raise re.error("private pattern")

    monkeypatch.setattr(local_nodes.PatternMatcher, "matches", broken)
    (source / "main.py").write_bytes(b"code")
    with pytest.raises(LocalInputAdmissionError) as raised:
        _bundle(tmp_path)
    assert "private pattern" not in str(raised.value)
