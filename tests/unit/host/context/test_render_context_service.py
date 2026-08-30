"""Canonical Host render-context service behavior."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from tests.host_render_service_support import (
    FakeAcquirer,
    _config,
    _prepare,
    _runtime_hooks,
    _tree,
)

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
    config = tmp_path / "config.toml"
    config.write_text(_config())
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


def test_check_unlocked_local_file_rejects_size_before_reading_bytes(
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
'''
    )
    output = tmp_path / "context"
    prepared = _prepare(config, output, FakeAcquirer())
    context_file = output / prepared.plan.files.files[0].context_path
    context_file.write_bytes(b"different size")

    monkeypatch.setattr(
        render_service_module.AdmittedRegularFileReader,
        "read_chunk",
        lambda *_args, **_kwargs: pytest.fail(
            "size mismatch must not consume file bytes"
        ),
    )
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            options=PlanningOptions(check=True),
        )

    assert raised.value.diagnostics[0].code == "render.context_changed"


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
