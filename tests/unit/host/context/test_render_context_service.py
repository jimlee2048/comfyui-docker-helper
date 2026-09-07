"""Canonical Host render-context service behavior."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from tests.host_render_service_support import (
    FakeAcquirer,
    _config,
    _prepare,
    _runtime_hooks,
    _tree,
)

from comfyui_docker_helper.filesystem import admission as file_admission
from comfyui_docker_helper.host.buildx import BuildxOutputPlan
from comfyui_docker_helper.host.context import service as render_service_module
from comfyui_docker_helper.host.context.service import (
    HostRenderServiceError,
    PlanningOptions,
)
from comfyui_docker_helper.host.presentation.events import (
    HostPhase,
    HostPhaseCompleted,
    HostPhaseStarted,
    HostSubphase,
    HostSubphaseCompleted,
    HostSubphaseStarted,
    HostWorkflowEvent,
)
from comfyui_docker_helper.rendering.final_materializer import (
    FinalMaterializationError,
)


@dataclass
class RecordingHostEvents:
    events: list[HostWorkflowEvent] = field(default_factory=list)

    def emit(self, event: HostWorkflowEvent, /) -> None:
        self.events.append(event)


def test_render_service_emits_one_truthful_coarse_phase_sequence(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    events = RecordingHostEvents()

    _prepare(config, tmp_path / "output", FakeAcquirer(), event_sink=events)

    assert events.events == [
        HostPhaseCompleted(HostPhase.BUILD_INPUT_RESOLUTION),
        HostPhaseStarted(HostPhase.LOCK_RECONCILIATION),
        HostSubphaseStarted(HostSubphase.CANONICAL_IDENTITY_RECONCILIATION),
        HostSubphaseCompleted(HostSubphase.CANONICAL_IDENTITY_RECONCILIATION),
        HostPhaseCompleted(HostPhase.LOCK_RECONCILIATION),
        HostPhaseStarted(HostPhase.BUILD_PLAN_PREPARATION),
        HostPhaseCompleted(HostPhase.BUILD_PLAN_PREPARATION),
        HostPhaseStarted(HostPhase.CONTEXT_RENDER_CHECK),
    ]


def test_render_service_dry_run_returns_with_plan_active_and_no_context(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    events = RecordingHostEvents()

    _prepare(
        config,
        tmp_path / "output",
        FakeAcquirer(),
        options=PlanningOptions(dry_run=True),
        event_sink=events,
    )

    assert events.events == [
        HostPhaseCompleted(HostPhase.BUILD_INPUT_RESOLUTION),
        HostPhaseStarted(HostPhase.LOCK_RECONCILIATION),
        HostSubphaseStarted(HostSubphase.CANONICAL_IDENTITY_RECONCILIATION),
        HostSubphaseCompleted(HostSubphase.CANONICAL_IDENTITY_RECONCILIATION),
        HostPhaseCompleted(HostPhase.LOCK_RECONCILIATION),
        HostPhaseStarted(HostPhase.BUILD_PLAN_PREPARATION),
    ]


@pytest.mark.parametrize(
    "options",
    [PlanningOptions(locked=True), PlanningOptions(check=True)],
)
def test_publication_only_config_changes_do_not_make_context_stale(
    tmp_path: Path,
    options: PlanningOptions,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    initial = _prepare(
        config,
        output,
        FakeAcquirer(),
        tag_templates=("example:test",),
    )
    before = _tree(output)
    config.write_text(
        _config().replace(
            'tags = ["example:test"]',
            'tags = ["cli:first", "cli:second"]\noutput = "push"',
        )
    )
    fake = FakeAcquirer()
    prepared = _prepare(
        config,
        output,
        fake,
        options=options,
        tag_templates=("cli:first", "cli:second"),
        output_mode="push",
    )

    assert initial.output_plan == BuildxOutputPlan(
        tags=("example:test",), output="load"
    )
    assert prepared.output_plan == BuildxOutputPlan(
        tags=("cli:first", "cli:second"), output="push"
    )
    assert fake.calls == []
    assert _tree(output) == before


def test_dry_run_resolves_without_writing_and_check_compares_without_writing(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    dry_output = tmp_path / "dry"
    dry = _prepare(
        config,
        dry_output,
        FakeAcquirer(),
        options=PlanningOptions(dry_run=True),
    )
    assert dry.lock_result.changed
    assert not dry_output.exists()

    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    before = _tree(output)
    _prepare(config, output, FakeAcquirer(), options=PlanningOptions(check=True))
    assert _tree(output) == before
    (output / "Dockerfile").write_text("changed")
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer(), options=PlanningOptions(check=True))
    assert raised.value.diagnostics[0].code == "render.context_changed"


# Host owns private-stage creation, platform privacy, and whole-stage cleanup.
def test_host_passes_fresh_output_sibling_private_stages_to_materializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    original = render_service_module._materialize_private_stage
    observed_stages: list[Path] = []
    observed_phases: set[str] = set()
    phase = "normal"

    def inspect_stage(plan, directory, **kwargs):
        stage = Path(directory)
        assert stage.parent == output.parent
        assert stage != output
        assert stage not in observed_stages
        if os.name == "posix":
            assert stage.stat().st_mode & 0o777 == 0o700
        assert tuple(stage.iterdir()) == ()
        observed_stages.append(stage)
        observed_phases.add(phase)
        return original(plan, stage, **kwargs)

    monkeypatch.setattr(
        render_service_module, "_materialize_private_stage", inspect_stage
    )
    _prepare(config, output, FakeAcquirer())

    phase = "check"
    _prepare(
        config,
        output,
        FakeAcquirer(),
        options=PlanningOptions(check=True),
    )
    assert observed_phases == {"normal", "check"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX rendered-mode contract")
def test_host_context_modes_are_deterministic_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty-tree"
    source.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + f'''
[[files]]
type = "local"
source = "{source.as_posix()}"
target = "user/default/workflows"
'''
    )
    hooks = _runtime_hooks(tmp_path / "hooks")
    output = tmp_path / "context"
    previous_umask = os.umask(0o077)
    try:
        _prepare(config, output, FakeAcquirer(), runtime_hooks_dir=hooks)
    finally:
        os.umask(previous_umask)

    assert output.stat().st_mode & 0o777 == 0o700
    for path in output.rglob("*"):
        permissions = path.stat().st_mode & 0o777
        if path.is_dir() or output / "runtime/hooks" in path.parents:
            assert permissions == 0o755
        else:
            assert permissions == 0o644


def test_host_removes_partial_private_stage_after_materializer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    observed_stages: list[Path] = []

    def fail_after_partial_write(_plan, directory, **_kwargs):
        stage = Path(directory)
        observed_stages.append(stage)
        (stage / "partial").write_text("partial")
        raise FinalMaterializationError("materialization failed")

    monkeypatch.setattr(
        render_service_module,
        "_materialize_private_stage",
        fail_after_partial_write,
    )

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer())

    assert raised.value.diagnostics[0].code == "render.context_write_failed"
    assert not output.exists()
    assert observed_stages
    assert all(not stage.exists() for stage in observed_stages)


@pytest.mark.parametrize("name", ["config.lock.toml", ".cdh-rendered"])
def test_host_metadata_writes_are_exclusive_and_clean_failed_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    original = render_service_module._materialize_private_stage
    observed_stages: list[Path] = []

    def create_collision(plan, directory, **kwargs):
        stage = Path(directory)
        observed_stages.append(stage)
        original(plan, stage, **kwargs)
        (stage / name).write_text("collision")

    monkeypatch.setattr(
        render_service_module, "_materialize_private_stage", create_collision
    )

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer())

    assert raised.value.diagnostics[0].code == "render.context_write_failed"
    assert not output.exists()
    assert observed_stages
    assert all(not stage.exists() for stage in observed_stages)


def test_windows_metadata_write_and_check_tree_do_not_use_posix_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "expected"
    observed = tmp_path / "observed"
    expected.mkdir()
    observed.mkdir()
    (expected / "payload").write_bytes(b"same")
    (observed / "payload").write_bytes(b"same")
    (expected / "payload").chmod(0o600)
    (observed / "payload").chmod(0o755)

    monkeypatch.setattr(render_service_module, "_platform_name", "nt")
    monkeypatch.setattr(
        render_service_module.os,
        "fchmod",
        lambda *_args: pytest.fail("Windows metadata write called fchmod"),
        raising=False,
    )

    render_service_module._write_private_stage_metadata(
        expected, "metadata", b"metadata"
    )
    (observed / "metadata").write_bytes(b"metadata")

    assert render_service_module._tree(expected) == render_service_module._tree(
        observed
    )


def test_render_output_real_directory_check_rejects_observed_reparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(
        render_service_module,
        "observed_path_is_reparse",
        lambda _observed: True,
    )

    assert render_service_module._is_real_directory(output) is False


def test_check_tree_does_not_descend_into_an_observed_reparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    linked = root / "linked"
    linked.mkdir(parents=True)
    (linked / "outside-sentinel").write_text("outside")
    monkeypatch.setattr(
        render_service_module,
        "observed_path_is_reparse",
        lambda _observed: True,
    )

    tree = render_service_module._tree(root)

    assert set(tree) == {"linked"}
    assert tree["linked"][0] == "symlink"
    assert tree["linked"][2] is None


# Check mode compares the complete path, content, and executable-mode result.
@pytest.mark.parametrize(
    "mutation",
    [
        "extra-dir",
        "missing-dir",
        "symlink",
        pytest.param(
            "special",
            marks=pytest.mark.skipif(
                os.name != "posix", reason="requires a POSIX FIFO"
            ),
        ),
    ],
)
def test_check_compares_complete_path_type_and_bytes_without_following(
    tmp_path: Path,
    mutation: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    outside = tmp_path / "outside"
    outside.write_text("outside sentinel")
    if mutation == "extra-dir":
        (output / "extra-empty").mkdir()
    elif mutation == "missing-dir":
        shutil.rmtree(output / "runtime")
    elif mutation == "symlink":
        (output / "Dockerfile").unlink()
        (output / "Dockerfile").symlink_to(outside)
    else:
        os.mkfifo(output / "extra-special")

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            options=PlanningOptions(check=True),
        )

    assert raised.value.diagnostics[0].code == "render.context_changed"
    assert outside.read_text() == "outside sentinel"


def test_check_unlocked_local_file_detects_same_size_byte_mismatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"original")
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + f'''
[cdh]
local_file_mode = "copy"

[[files]]
type = "local"
source = "{source.as_posix()}"
target = "models/model.bin"
'''
    )
    output = tmp_path / "context"
    prepared = _prepare(config, output, FakeAcquirer())
    context_file = output / prepared.plan.files.files[0].context_path
    context_file.write_bytes(b"changed!")

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            options=PlanningOptions(check=True),
        )

    assert raised.value.diagnostics[0].code == "render.context_changed"


def _local_tree_config(source: Path, *, content_lock: bool = False) -> str:
    return (
        _config()
        + f'''
[[files]]
type = "local"
source = "{source.as_posix()}"
target = "user/default/workflows"
content_lock = {str(content_lock).lower()}
'''
    )


@pytest.mark.parametrize(
    ("purpose", "locked", "source_passes", "context_passes"),
    [
        ("copy", False, 1, 0),
        ("copy", True, 2, 0),
        ("clone", False, 0, 0),
        ("clone", True, 1, 0),
        ("check", False, 1, 1),
        ("check", True, 1, 1),
        ("locked", False, 0, 0),
        ("locked", True, 1, 1),
        ("dry-run", False, 0, 0),
        ("dry-run", True, 1, 0),
    ],
)
def test_local_tree_preparation_reads_content_only_for_its_purpose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
    locked: bool,
    source_passes: int,
    context_passes: int,
) -> None:
    source = tmp_path / "tree"
    (source / "nested").mkdir(parents=True)
    payload = source / "nested" / "payload.bin"
    content = b"model content"
    payload.write_bytes(content)
    config = tmp_path / "config.toml"
    mode = "clone" if purpose == "clone" else "copy"
    config.write_text(
        _local_tree_config(source, content_lock=locked)
        + f'\n[cdh]\nlocal_file_mode = "{mode}"\n'
    )
    output = tmp_path / "context"
    context_file = None
    if purpose in {"check", "locked"}:
        prepared = _prepare(config, output, FakeAcquirer())
        context_file = (
            output / prepared.plan.files.files[0].context_path / "nested/payload.bin"
        )
    read_bytes = {"source": 0, "context": 0}
    operate = file_admission._operate_regular_absolute_file

    def observe(path, operation):
        key = (
            "source"
            if Path(path) == payload
            else "context"
            if Path(path) == context_file
            else None
        )
        if key is None:
            return operate(path, operation)

        def count_operation(reader):
            def read(limit=None):
                chunk = reader.read_chunk(limit)
                read_bytes[key] += len(chunk)
                return chunk

            return operation(replace(reader, _read_chunk=read))

        return operate(path, count_operation)

    def clone_result(_reader, destination_fd):
        assert os.write(destination_fd, content) == len(content)

    monkeypatch.setattr(file_admission, "_operate_regular_absolute_file", observe)
    if purpose == "clone":
        monkeypatch.setattr(
            file_admission.AdmittedRegularFileReader, "clone_to", clone_result
        )
    options = PlanningOptions(
        check=purpose == "check",
        locked=purpose == "locked",
        dry_run=purpose == "dry-run",
    )

    prepared = _prepare(config, output, FakeAcquirer(), options=options)

    assert read_bytes == {
        "source": source_passes * len(content),
        "context": context_passes * len(content),
    }
    if purpose == "dry-run":
        assert not output.exists()
    else:
        context_file = (
            output / prepared.plan.files.files[0].context_path / "nested/payload.bin"
        )
        assert context_file.read_bytes() == content


@pytest.mark.parametrize("overwrite", [False, True])
def test_late_unlocked_read_failure_does_not_publish_partial_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overwrite: bool,
) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    payload = source / "payload.bin"
    payload.write_bytes(b"original")
    config = tmp_path / "config.toml"
    config.write_text(
        _local_tree_config(source) + '\n[cdh]\nlocal_file_mode = "copy"\n'
    )
    output = tmp_path / "context"
    if overwrite:
        _prepare(config, output, FakeAcquirer())
    before = _tree(output) if overwrite else None
    before_paths = set(tmp_path.iterdir())
    operate = file_admission._operate_regular_absolute_file
    consumed = 0

    def observe(path, operation):
        if Path(path) != payload:
            return operate(path, operation)

        def fail_midstream(reader):
            def read(_limit=None):
                nonlocal consumed
                if consumed:
                    raise OSError("synthetic midstream failure")
                chunk = reader.read_chunk(4)
                consumed += len(chunk)
                return chunk

            return operation(replace(reader, _read_chunk=read))

        return operate(path, fail_midstream)

    monkeypatch.setattr(file_admission, "_operate_regular_absolute_file", observe)

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer(), overwrite=overwrite)

    assert consumed == 4
    assert raised.value.diagnostics[0].code == "render.context_write_failed"
    assert set(tmp_path.iterdir()) == before_paths
    if overwrite:
        assert _tree(output) == before
    else:
        assert not output.exists()


def test_check_unlocked_local_tree_streams_same_size_member_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "payload.bin").write_bytes(b"original")
    config = tmp_path / "config.toml"
    config.write_text(_local_tree_config(source))
    output = tmp_path / "context"
    prepared = _prepare(config, output, FakeAcquirer())
    context_file = (
        output / prepared.plan.files.files[0].context_path / "nested" / "payload.bin"
    )
    context_file.write_bytes(b"changed!")

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            options=PlanningOptions(check=True),
        )

    assert raised.value.diagnostics[0].code == "render.context_changed"


def test_locked_mode_freezes_unlocked_local_tree_structure_without_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "tree"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "payload.bin").write_bytes(b"original")
    config = tmp_path / "config.toml"
    config.write_text(_local_tree_config(source))
    output = tmp_path / "context"
    prepared = _prepare(config, output, FakeAcquirer())
    context_root = output / prepared.plan.files.files[0].context_path
    context_file = context_root / "nested" / "payload.bin"
    context_file.write_bytes(b"changed!")
    before = _tree(output)
    monkeypatch.setattr(
        render_service_module,
        "_regular_files_equal",
        lambda *_args: pytest.fail("unlocked --locked must not compare file bytes"),
    )

    _prepare(
        config,
        output,
        FakeAcquirer(),
        options=PlanningOptions(locked=True),
    )
    (source / "empty").mkdir()

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            options=PlanningOptions(locked=True),
        )

    assert raised.value.diagnostics[0].code == "render.context_changed"
    assert _tree(output) == before
    assert not (context_root / "empty").exists()


def test_check_locked_local_tree_hashes_context_members_against_plan(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "payload.bin").write_bytes(b"original")
    config = tmp_path / "config.toml"
    config.write_text(_local_tree_config(source, content_lock=True))
    output = tmp_path / "context"
    prepared = _prepare(config, output, FakeAcquirer())
    context_file = (
        output / prepared.plan.files.files[0].context_path / "nested" / "payload.bin"
    )
    context_file.write_bytes(b"changed!")

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            options=PlanningOptions(check=True),
        )

    assert raised.value.diagnostics[0].code == "render.context_changed"


def test_empty_local_tree_warnings_are_once_and_in_effective_order(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first-tree"
    second = tmp_path / "second-tree"
    first.mkdir()
    second.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + f'''
[[files]]
type = "local"
source = "{first.as_posix()}"
target = "user/default/first"

[[files]]
type = "local"
source = "{second.as_posix()}"
target = "user/default/second"
'''
    )
    output = tmp_path / "context"

    prepared = _prepare(config, output, FakeAcquirer())
    expected_paths = [("files", 0, "source"), ("files", 1, "source")]
    assert [warning.path for warning in prepared.warnings] == expected_paths
    assert [warning.code for warning in prepared.warnings] == [
        "render.local_source_empty",
        "render.local_source_empty",
    ]

    checked = _prepare(
        config,
        output,
        FakeAcquirer(),
        options=PlanningOptions(check=True),
    )
    assert [warning.path for warning in checked.warnings] == expected_paths

    dry_run = _prepare(
        config,
        tmp_path / "dry-run",
        FakeAcquirer(),
        options=PlanningOptions(dry_run=True),
    )
    assert [warning.path for warning in dry_run.warnings] == expected_paths


def test_local_file_comparison_rejects_size_mismatch_without_reading_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    context = tmp_path / "context.bin"
    source.write_bytes(b"a")
    context.write_bytes(b"bb")
    monkeypatch.setattr(
        render_service_module.AdmittedRegularFileReader,
        "read_chunk",
        lambda *_args: pytest.fail("different file sizes need no byte comparison"),
    )

    assert not render_service_module._regular_files_equal(source, context)


def test_unlocked_local_file_comparison_ignores_short_read_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("/source.bin")
    context = Path("/context.bin")
    chunks = {
        source: [b"a", b"bc", b"def", b""],
        context: [b"ab", b"cdef", b""],
    }

    def operate(path: Path, operation):
        reads = iter(chunks[path])
        reader = render_service_module.AdmittedRegularFileReader(
            6,
            None,
            lambda _limit=None: next(reads, b""),
        )
        return operation(reader)

    monkeypatch.setattr(
        render_service_module,
        "operate_regular_absolute_file",
        operate,
    )

    assert render_service_module._regular_files_equal(source, context)


def test_check_locked_local_file_hashes_context_against_intended_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"original")
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + f'''
[cdh]
local_file_mode = "copy"

[[files]]
type = "local"
source = "{source.as_posix()}"
target = "models/model.bin"
content_lock = true
'''
    )
    output = tmp_path / "context"
    prepared = _prepare(config, output, FakeAcquirer())
    context_file = output / prepared.plan.files.files[0].context_path
    context_file.write_bytes(b"changed!")
    checked: list[Path] = []
    consume = render_service_module.consume_regular_absolute_file

    def observe(path: Path, callback) -> object:
        checked.append(path)
        return consume(path, callback)

    monkeypatch.setattr(
        render_service_module,
        "consume_regular_absolute_file",
        observe,
    )
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            options=PlanningOptions(check=True),
        )

    assert raised.value.diagnostics[0].code == "render.context_changed"
    assert checked == [context_file]


@pytest.mark.skipif(os.name != "posix", reason="POSIX rendered-mode contract")
def test_check_detects_materialized_hook_permission_drift(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = _runtime_hooks(tmp_path / "hooks")
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer(), runtime_hooks_dir=hooks)
    rendered = output / "runtime/hooks/pre-start.d/10-pre.sh"
    assert rendered.stat().st_mode & 0o777 == 0o755
    rendered.chmod(0o644)

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            runtime_hooks_dir=hooks,
            options=PlanningOptions(check=True),
        )

    assert raised.value.diagnostics[0].code == "render.context_changed"


@pytest.mark.parametrize("locked", [False, True])
def test_local_node_dry_run_prepares_independent_lock_and_plan(
    tmp_path: Path, locked: bool
) -> None:
    source = tmp_path / "node"
    source.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + f"""
[[comfyui.custom_nodes]]
type = "local"
source = "node"
target_dir = "example"
content_lock = {str(locked).lower()}
"""
    )
    output = tmp_path / "context"
    prepared = _prepare(
        config, output, FakeAcquirer(), options=PlanningOptions(dry_run=True)
    )
    node = prepared.plan.custom_nodes.nodes[0]
    assert node.type == "local"
    assert node.members == ()
    assert (node.tree_digest is not None) == locked
    assert len(prepared.lock_result.lock.custom_nodes.local) == int(locked)
    assert len(prepared.warnings) == 1
    assert prepared.warnings[0].path == ("comfyui", "custom_nodes", 0, "source")
    assert not output.exists()
