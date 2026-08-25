"""Canonical Host render-context publication behavior."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from tests.host_render_service_support import (
    COMMIT,
    FakeAcquirer,
    _config,
    _prepare,
    _tree,
)

from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.host.buildx import BuildxOutputPlan
from comfyui_docker_helper.host.context import service as render_service_module
from comfyui_docker_helper.host.context.service import (
    HostRenderServiceError,
    PlanningOptions,
)


def _valid_context(output: Path) -> bool:
    return (output / ".cdh-rendered").is_file() and (output / "Dockerfile").is_file()


def test_publication_templates_expand_from_the_accepted_comfyui_identity(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())

    prepared = _prepare(
        config,
        tmp_path / "context",
        FakeAcquirer(),
        tag_templates=(
            "example/comfyui:latest",
            "example/comfyui:v${{ comfyui.release }}",
            "example/comfyui:custom-${{ comfyui.commit.prefix(12) }}",
        ),
        output_mode="push",
    )

    assert prepared.output_plan == BuildxOutputPlan(
        tags=(
            "example/comfyui:latest",
            "example/comfyui:v0.11.0",
            f"example/comfyui:custom-{COMMIT[:12]}",
        ),
        output="push",
    )


@pytest.mark.parametrize("options", [PlanningOptions(), PlanningOptions(check=True)])
def test_missing_release_fails_before_context_write_or_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: PlanningOptions,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config().replace('version = "0.11.0"', 'version = "nightly"'))
    monkeypatch.setattr(
        render_service_module,
        "_write_context",
        lambda *args, **kwargs: pytest.fail("tag resolution must precede writing"),
    )
    monkeypatch.setattr(
        render_service_module,
        "_check_context",
        lambda *args, **kwargs: pytest.fail("tag resolution must precede checking"),
    )

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            tmp_path / "context",
            FakeAcquirer(formal_release=None),
            tag_templates=("example/comfyui:v${{ comfyui.release }}",),
            options=options,
        )

    assert raised.value.diagnostics == (
        Diagnostic(
            ("build", "tags", 0),
            "build.release_unavailable",
            "comfyui.release is unavailable for this ComfyUI selector",
        ),
    )


# Context replacement owns unique staging/backup paths and preserves foreign siblings.
def test_overwrite_uses_unique_owned_backup_and_preserves_sibling(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    sibling = tmp_path / ".context.previous"
    sibling.mkdir()
    sentinel = sibling / "sentinel"
    sentinel.write_text("keep")
    backup_sibling = tmp_path / f"{render_service_module._BACKUP_PREFIX}user"
    backup_sibling.mkdir()
    backup_sentinel = backup_sibling / "sentinel"
    backup_sentinel.write_text("also keep")

    _prepare(config, output, FakeAcquirer(), overwrite=True)

    assert sentinel.read_text() == "keep"
    assert backup_sentinel.read_text() == "also keep"
    assert list(tmp_path.glob(f"{render_service_module._BACKUP_PREFIX}*")) == [
        backup_sibling
    ]


def test_stage_rename_failure_restores_existing_context_and_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    before = _tree(output)
    sibling = tmp_path / ".context.previous"
    sibling.write_text("keep")
    original_rename = Path.rename

    def fail_stage(self: Path, target: Path):
        if (
            self.name.startswith(render_service_module._STAGE_PREFIX)
            and Path(target) == output
        ):
            raise OSError("stage rename denied")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_stage)
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer(), overwrite=True)

    assert raised.value.diagnostics[0].code == "render.context_write_failed"
    assert _tree(output) == before
    assert sibling.read_text() == "keep"
    assert not list(tmp_path.glob(f"{render_service_module._BACKUP_PREFIX}*"))


def test_owned_backup_cleanup_failure_never_touches_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    sibling = tmp_path / ".context.previous"
    sibling.write_text("keep")
    original_rmtree = shutil.rmtree

    def fail_backup(path, *args, **kwargs):
        if Path(path).name.startswith(render_service_module._BACKUP_PREFIX):
            raise OSError("cleanup denied")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", fail_backup)
    _prepare(config, output, FakeAcquirer(), overwrite=True)

    assert sibling.read_text() == "keep"
    assert _valid_context(output)
    assert len(list(tmp_path.glob(f"{render_service_module._BACKUP_PREFIX}*"))) == 1


def test_restore_rename_failure_retains_original_in_owned_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    before = _tree(output)
    sibling = tmp_path / ".context.previous"
    sibling.write_text("keep")
    original_rename = Path.rename
    previous_path: Path | None = None
    stage_path: Path | None = None

    def fail_stage_and_restore(self: Path, target: Path):
        nonlocal previous_path, stage_path
        target = Path(target)
        if self == output:
            result = original_rename(self, target)
            previous_path = target
            return result
        if target == output and previous_path is not None:
            if self == previous_path:
                raise OSError("restore rename denied")
            stage_path = self
            raise OSError("stage rename denied")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_stage_and_restore)
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer(), overwrite=True)

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "render.context_restore_failed"
    assert previous_path is not None
    assert stage_path is not None
    retained = previous_path
    assert "stage rename denied" in diagnostic.message
    assert "restore rename denied" in diagnostic.message
    assert str(retained) in diagnostic.message
    assert raised.value.__cause__ is not None
    assert str(raised.value.__cause__) == "restore rename denied"
    assert str(raised.value.__cause__.__context__) == "stage rename denied"
    assert not output.exists()
    assert _tree(retained) == before
    assert not stage_path.exists()
    assert sibling.read_text() == "keep"


def test_unexpected_publication_failure_restores_output_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedPublicationFailure(BaseException):
        pass

    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    before = _tree(output)
    original_rename = Path.rename
    previous_path: Path | None = None
    stage_path: Path | None = None
    restore_attempted = False

    def interrupt_stage(self: Path, target: Path):
        nonlocal previous_path, restore_attempted, stage_path
        target = Path(target)
        if self == output:
            result = original_rename(self, target)
            previous_path = target
            return result
        if target == output and previous_path is not None:
            if self == previous_path:
                restore_attempted = True
                return original_rename(self, target)
            stage_path = self
            raise UnexpectedPublicationFailure("publication interrupted")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", interrupt_stage)
    with pytest.raises(UnexpectedPublicationFailure, match="publication interrupted"):
        _prepare(config, output, FakeAcquirer(), overwrite=True)

    assert restore_attempted
    assert previous_path is not None
    assert stage_path is not None
    assert _tree(output) == before
    assert not previous_path.parent.exists()
    assert not stage_path.exists()


def test_context_parent_filesystem_failure_is_stable_render_diagnostic(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    parent = tmp_path / "not-a-directory"
    parent.write_text("sentinel")

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, parent / "context", FakeAcquirer())

    assert raised.value.diagnostics[0].code == "render.context_write_failed"
    assert parent.read_text() == "sentinel"
