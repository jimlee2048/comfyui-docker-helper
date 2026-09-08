"""Local-node snapshots cross materialization and context comparison boundaries."""

import os
import shutil
from pathlib import Path

import pytest
from tests.host_render_service_support import FakeAcquirer, _config, _prepare, _tree

from comfyui_docker_helper.config.planning.build_plan import LocalNodePlan
from comfyui_docker_helper.filesystem import admission
from comfyui_docker_helper.host.context import service
from comfyui_docker_helper.host.context.service import (
    HostRenderServiceError,
    PlanningOptions,
)
from comfyui_docker_helper.rendering import final_materializer as materializer


def _inputs(tmp_path: Path, *, locked: bool = False, rules: bytes | None = b"cache\n"):
    source = tmp_path / "node"
    (source / "cache").mkdir(parents=True)
    (source / "cache" / "ignored.py").write_bytes(b"ignored")
    (source / "empty").mkdir()
    (source / "main.py").write_bytes(b"original")
    if rules is not None:
        (source / ".dockerignore").write_bytes(rules)
    config = tmp_path / "config.toml"
    config.write_text(
        _config(install_cli=False)
        + f"""
[[comfyui.custom_nodes]]
type = "local"
source = "node"
target_dir = "example"
content_lock = {str(locked).lower()}
"""
    )
    return config, source, tmp_path / "context"


@pytest.mark.parametrize("locked", [False, True])
def test_local_node_context_is_independent_and_self_contained(tmp_path: Path, locked):
    config, source, output = _inputs(tmp_path, locked=locked)
    prepared = _prepare(config, output, FakeAcquirer())
    node = prepared.plan.custom_nodes.nodes[0]
    assert isinstance(node, LocalNodePlan)
    snapshot = output / node.context_path
    assert snapshot.parts[-3:-1] == ("build", "local-nodes")
    assert not (snapshot / "cache").exists()
    assert (snapshot / "empty").is_dir()
    assert (snapshot / ".dockerignore").read_bytes() == b"cache\n"
    (source / "main.py").write_bytes(b"changed")
    assert (snapshot / "main.py").read_bytes() == b"original"
    shutil.rmtree(source)
    assert (snapshot / "main.py").read_bytes() == b"original"
    assert str(source) not in (output / "build-plan.json").read_text()
    if os.name == "posix":
        assert snapshot.stat().st_mode & 0o777 == 0o755
        assert (snapshot / "empty").stat().st_mode & 0o777 == 0o755
        assert (snapshot / "main.py").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "member,changed",
    [("main.py", b"modified"), (".dockerignore", b"# new comment\ncache\n")],
    ids=["ordinary-source", "rule-comments"],
)
def test_unlocked_modes_stream_check_but_locked_preserves_old_bytes(
    tmp_path: Path, monkeypatch, member, changed
):
    config, source, output = _inputs(tmp_path)
    prepared = _prepare(config, output, FakeAcquirer())
    original = _tree(output)
    original_bytes = (source / member).read_bytes()
    (source / member).write_bytes(changed)

    def no_copy(*_args, **_kwargs):
        pytest.fail("no-write comparison copied a local file")

    monkeypatch.setattr(materializer, "_materialize_regular_file", no_copy)
    _prepare(config, output, FakeAcquirer(), options=PlanningOptions(locked=True))
    assert _tree(output) == original
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer(), options=PlanningOptions(check=True))
    assert raised.value.diagnostics[0].code == "render.context_changed"
    assert _tree(output) == original
    (source / member).write_bytes(original_bytes)
    (source / "cache" / "ignored.py").write_bytes(b"ignored change")
    checked = _prepare(
        config, output, FakeAcquirer(), options=PlanningOptions(check=True)
    )
    assert checked.plan == prepared.plan


@pytest.mark.parametrize("locked_mode", [False, True], ids=["check", "locked"])
def test_context_complete_inventory_never_applies_node_ignore(
    tmp_path: Path, locked_mode
):
    config, _source, output = _inputs(tmp_path)
    prepared = _prepare(config, output, FakeAcquirer())
    snapshot = output / prepared.plan.custom_nodes.nodes[0].context_path
    (snapshot / "cache").mkdir()
    (snapshot / "cache" / "injected.py").write_bytes(b"extra")
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            options=PlanningOptions(locked=locked_mode, check=not locked_mode),
        )
    assert raised.value.diagnostics[0].code == "render.context_changed"


@pytest.mark.parametrize("where", ["source", "context"])
def test_locked_node_content_drift_fails(tmp_path: Path, where):
    config, source, output = _inputs(tmp_path, locked=True)
    prepared = _prepare(config, output, FakeAcquirer())
    root = (
        source
        if where == "source"
        else (output / prepared.plan.custom_nodes.nodes[0].context_path)
    )
    (root / "main.py").write_bytes(b"modified")
    with pytest.raises(HostRenderServiceError):
        _prepare(config, output, FakeAcquirer(), options=PlanningOptions(locked=True))


@pytest.mark.parametrize(
    "mode,change",
    [
        ("apply", "bytes"),
        ("apply", "removed"),
        ("apply", "created"),
        ("check", "bytes"),
        ("locked", "bytes"),
    ],
)
def test_control_drift_after_admission_fails_without_publishing(
    tmp_path: Path, monkeypatch, change, mode
):
    config, source, output = _inputs(
        tmp_path, rules=None if change == "created" else b"cache\n"
    )
    _prepare(config, output, FakeAcquirer())
    original = _tree(output)
    admit = service.admit_local_node_inputs

    def admit_then_change(*args, **kwargs):
        bundle = admit(*args, **kwargs)
        control = source / ".dockerignore"
        if change == "removed":
            control.unlink()
        else:
            control.write_bytes(b"# changed\ncache\n")
        return bundle

    monkeypatch.setattr(service, "admit_local_node_inputs", admit_then_change)
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            overwrite=True,
            options=PlanningOptions(check=mode == "check", locked=mode == "locked"),
        )
    assert "control file changed" in raised.value.diagnostics[0].message
    assert str(source) not in raised.value.diagnostics[0].message
    assert _tree(output) == original
    assert not list(tmp_path.glob("cdh-render-*"))


def test_control_copied_bytes_must_match_snapshot_even_if_source_restored(
    tmp_path: Path, monkeypatch
):
    config, source, output = _inputs(tmp_path)
    copy = materializer._materialize_regular_file

    def copy_changed_rule(stage, relative_path, path, **kwargs):
        if path.name == ".dockerignore":
            path.write_bytes(b"# temporary change\ncache\n")
        copy(stage, relative_path, path, **kwargs)
        if path.name == ".dockerignore":
            path.write_bytes(b"cache\n")

    monkeypatch.setattr(materializer, "_materialize_regular_file", copy_changed_rule)
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer())
    assert "control file changed" in raised.value.diagnostics[0].message
    assert (source / ".dockerignore").read_bytes() == b"cache\n"
    assert not output.exists()
    assert not list(tmp_path.glob("cdh-render-*"))


def test_rule_drift_after_copy_is_rejected_at_final_source_boundary(
    tmp_path: Path, monkeypatch
):
    config, source, output = _inputs(tmp_path)
    copy = materializer._materialize_regular_file

    def copy_then_change(stage, relative_path, path, **kwargs):
        copy(stage, relative_path, path, **kwargs)
        if path.name == "main.py":
            (source / ".dockerignore").write_bytes(b"# changed\ncache\n")

    monkeypatch.setattr(materializer, "_materialize_regular_file", copy_then_change)
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer())
    assert "control file changed" in raised.value.diagnostics[0].message
    assert not output.exists()
    assert not list(tmp_path.glob("cdh-render-*"))


def test_copied_rule_read_failure_has_a_controlled_diagnostic(
    tmp_path: Path, monkeypatch
):
    config, _source, output = _inputs(tmp_path)
    read = materializer.read_regular_absolute_file

    def read_control(path):
        if "local-nodes" in path.parts:
            raise OSError("private stage path")
        return read(path)

    monkeypatch.setattr(materializer, "read_regular_absolute_file", read_control)
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer())
    diagnostic = raised.value.diagnostics[0]
    assert "control file could not be checked" in diagnostic.message
    assert "private stage path" not in diagnostic.message
    assert not output.exists()


def test_equivalent_node_source_relocation_preserves_context_identity(tmp_path: Path):
    config, source, output = _inputs(tmp_path, locked=True)
    prepared = _prepare(config, output, FakeAcquirer())
    source.rename(tmp_path / "relocated")
    config.write_text(
        config.read_text().replace('source = "node"', 'source = "relocated"')
    )
    checked = _prepare(
        config, output, FakeAcquirer(), options=PlanningOptions(check=True)
    )
    assert checked.plan == prepared.plan


def test_dry_run_reads_only_rules_and_never_materializes(tmp_path: Path, monkeypatch):
    config, _source, output = _inputs(tmp_path)

    def no_materialize(*_args, **_kwargs):
        pytest.fail("dry-run materialized a context")

    operate = admission.operate_regular_absolute_file

    def metadata_only(path, operation):
        def guarded(reader):
            if Path(path).name == "main.py":

                def forbidden(_limit=None):
                    pytest.fail("unlocked dry-run consumed node file bytes")

                reader = admission.AdmittedRegularFileReader(
                    reader.size, reader.mode, forbidden
                )
            return operation(reader)

        return operate(path, guarded)

    monkeypatch.setattr(admission, "operate_regular_absolute_file", metadata_only)
    monkeypatch.setattr(service, "_materialize_private_stage", no_materialize)
    prepared = _prepare(
        config, output, FakeAcquirer(), options=PlanningOptions(dry_run=True)
    )
    assert prepared.plan.custom_nodes.nodes[0].tree_digest is None
    assert not output.exists()


def test_local_node_and_files_overlay_have_independent_context_slots(tmp_path: Path):
    config, source, output = _inputs(tmp_path)
    config.write_text(
        config.read_text()
        + """
[[files]]
type = "local"
source = "node"
target = "custom_nodes/example"
"""
    )
    prepared = _prepare(config, output, FakeAcquirer())
    node_root = output / prepared.plan.custom_nodes.nodes[0].context_path
    file_root = output / prepared.plan.files.files[0].context_path
    assert node_root != file_root
    assert not (node_root / "cache").exists()
    assert (file_root / "cache" / "ignored.py").read_bytes() == b"ignored"
    assert (source / "cache").exists()
