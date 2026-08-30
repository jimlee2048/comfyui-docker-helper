"""Canonical Host render-context inputs behavior."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from tests.host_render_service_support import (
    FakeAcquirer,
    _config,
    _prepare,
    _runtime_hooks,
    _tree,
)

from comfyui_docker_helper.config.planning.canonical_lock import (
    LocalExecutableLockEntry,
    canonical_entry_key,
    parse_canonical_lock_toml,
)
from comfyui_docker_helper.config.runtime.config import load_runtime_config
from comfyui_docker_helper.host.context.service import (
    HostRenderServiceError,
    PlanningOptions,
)


def test_local_requirements_parser_failure_leaves_no_partial_context(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    output = tmp_path / "context"
    config.write_text(_config())

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(requirements_content=b"--index-url https://example.test"),
        )

    assert raised.value.diagnostics[0].code == "comfyui.requirements_invalid"
    assert not output.exists()


# Runtime inputs preserve locked hooks, typed files, and baked precedence.
def test_runtime_hooks_are_locked_planned_and_materialized(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = _runtime_hooks(tmp_path / "hooks")
    output = tmp_path / "context"

    prepared = _prepare(config, output, FakeAcquirer(), runtime_hooks_dir=hooks)

    assert [hook.relative_path for hook in prepared.plan.runtime.hooks] == [
        "pre-start.d/10-pre.sh",
        "post-start.d/20-post.py",
        "stop.d/30-stop.sh",
    ]
    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    local_entries = [
        entry for entry in lock.entries if isinstance(entry, LocalExecutableLockEntry)
    ]
    assert [entry.relative_path for entry in local_entries] == [
        "post-start.d/20-post.py",
        "pre-start.d/10-pre.sh",
        "stop.d/30-stop.sh",
    ]
    assert (output / "runtime/hooks/pre-start.d/10-pre.sh").read_text() == "pre\n"
    assert (
        output / "runtime/hooks/post-start.d/20-post.py"
    ).read_text() == "print('post')\n"
    assert (output / "runtime/hooks/stop.d/30-stop.sh").read_text() == "stop\n"
    dockerfile = (output / "Dockerfile").read_text()
    assert (
        "COPY --chmod=0644 runtime/config.toml /opt/cdh/runtime/config.toml"
        in dockerfile
    )
    assert "COPY --chmod=0755 runtime/hooks /opt/cdh/runtime/hooks" in dockerfile


def test_runtime_file_projection_preserves_global_and_item_precedence(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + """
[cdh]
default_downloader = "httpx"
default_download_mode = "async"

[[files]]
type = "http"
source = "https://example.test/implicit.bin"
target = "models/implicit.bin"

[[files]]
type = "http"
source = "https://example.test/explicit.bin"
target = "models/explicit.bin"
downloader = "httpx"
download_mode = "async"
"""
    )
    output = tmp_path / "context"

    prepared = _prepare(config, output, FakeAcquirer())

    assert prepared.plan.files.downloader.default == "httpx"
    assert prepared.plan.files.default_download_mode == "async"
    assert [item.downloader_explicit for item in prepared.plan.files.files] == [
        False,
        True,
    ]
    assert [item.download_mode_explicit for item in prepared.plan.files.files] == [
        False,
        True,
    ]
    baked_runtime = load_runtime_config(
        baked_config_path=output / "runtime/config.toml",
        mounted_config_path=tmp_path / "missing.toml",
        environ={},
    )
    assert baked_runtime.config.cdh.default_downloader == "httpx"
    assert baked_runtime.config.cdh.default_download_mode == "async"

    runtime = load_runtime_config(
        baked_config_path=output / "runtime/config.toml",
        mounted_config_path=tmp_path / "missing.toml",
        environ={
            "CDH_DEFAULT_DOWNLOADER": "aria2",
            "CDH_DEFAULT_DOWNLOAD_MODE": "sync",
        },
    )
    assert runtime.config.cdh.default_downloader == "aria2"
    assert runtime.config.cdh.default_download_mode == "sync"


def test_locked_local_file_uses_first_config_parent_and_omits_locator(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "base"
    overlay_dir = tmp_path / "overlay"
    source = base_dir / "assets" / "model.bin"
    source.parent.mkdir(parents=True)
    overlay_dir.mkdir()
    source.write_bytes(b"local-model")
    base = base_dir / "config.toml"
    base.write_text(_config())
    overlay = overlay_dir / "files.toml"
    overlay.write_text(
        """
[[files]]
type = "local"
source = "assets/model.bin"
target = "models/model.bin"
content_lock = true
"""
    )
    output = tmp_path / "context"

    prepared = _prepare([base, overlay], output, FakeAcquirer())

    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    assert lock.files.local[0].digest == (
        f"sha256:{hashlib.sha256(b'local-model').hexdigest()}"
    )
    serialized = (output / "build-plan.json").read_text()
    assert "assets/model.bin" not in serialized
    assert prepared.plan.files.files[0].verification == "sha256"


@pytest.mark.parametrize("locator_kind", ["relative", "absolute"])
def test_unlocked_local_file_admits_both_locator_shapes_without_hashing(
    tmp_path: Path,
    locator_kind: str,
) -> None:
    config_dir = tmp_path / "configuration"
    source = config_dir / "assets" / "model.bin"
    source.parent.mkdir(parents=True)
    original = b"unlocked local model"
    source.write_bytes(original)
    locator = "assets/model.bin" if locator_kind == "relative" else source.as_posix()
    config = config_dir / "config.toml"
    config.write_text(
        _config()
        + f'''
[cdh]
local_file_mode = "copy"

[[files]]
type = "local"
source = "{locator}"
target = "models/model.bin"
'''
    )
    output = tmp_path / "context"

    prepared = _prepare(config, output, FakeAcquirer())
    local = prepared.plan.files.files[0]
    source.write_bytes(b"source changed after publication")

    assert local.verification == "unverified-local"
    assert (
        parse_canonical_lock_toml(
            (output / "config.lock.toml").read_bytes()
        ).files.local
        == ()
    )
    assert (output / local.context_path).read_bytes() == original


def test_locked_mode_does_not_compare_unlocked_local_source_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"initial unlocked bytes")
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
    _prepare(config, output, FakeAcquirer())
    source.write_bytes(b"changed unlocked bytes")

    _prepare(
        config,
        output,
        FakeAcquirer(),
        options=PlanningOptions(locked=True),
    )


def test_local_file_root_target_is_rejected_even_when_content_locked(
    tmp_path: Path,
) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"local-model")
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + f'''
[[files]]
type = "local"
source = "{source.as_posix()}"
target = "."
content_lock = true
'''
    )

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, tmp_path / "context", FakeAcquirer())

    assert raised.value.diagnostics[0].code == "render.local_file_target_invalid"


def test_local_file_source_must_not_overlap_rendered_context(tmp_path: Path) -> None:
    output = tmp_path / "context"
    source = output / "model.bin"
    source.parent.mkdir()
    source.write_bytes(b"local-model")
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + f"""
[[files]]
type = "local"
source = "{source.as_posix()}"
target = "models/model.bin"
"""
    )

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer())

    assert raised.value.diagnostics[0].code == "render.input_output_overlap"


def test_invalid_local_file_locator_is_a_content_safe_render_diagnostic(
    tmp_path: Path,
) -> None:
    marker = "review-sensitive-locator"
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + f"""
[[files]]
type = "local"
source = "\\u0000{marker}"
target = "models/model.bin"
"""
    )

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, tmp_path / "context", FakeAcquirer())

    assert raised.value.diagnostics[0].code == "render.input_output_inspect_failed"
    assert marker not in str(raised.value)


@pytest.mark.parametrize("source_kind", ["build", "runtime"])
@pytest.mark.parametrize("relation", ["equal", "output-descendant", "output-ancestor"])
def test_render_rejects_every_source_output_overlap_before_overwrite(
    tmp_path: Path,
    source_kind: str,
    relation: str,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / ("build-hooks" if source_kind == "build" else "runtime-hooks")
    workspace.mkdir()
    if source_kind == "build":
        hook = source / "hook.sh"
        hook.parent.mkdir()
        hook.write_text("sentinel\n")
        config_text = (
            _config()
            + """
[[comfyui.custom_nodes]]
type = "git"
url = "https://example.test/node.git"
ref = "1111111111111111111111111111111111111111"
pre_install_hooks = ["hook.sh"]
"""
        )
        build_hooks_dir = source
        runtime_hooks_dir = None
    else:
        hook = source / "pre-start.d/10-hook.sh"
        hook.parent.mkdir(parents=True)
        hook.write_text("sentinel\n")
        config_text = _config()
        build_hooks_dir = tmp_path / "unused-build-hooks"
        runtime_hooks_dir = source
    hook.chmod(0o644)
    config = tmp_path / f"{source_kind}.toml"
    config.write_text(config_text)
    output = {
        "equal": source,
        "output-descendant": source / "context",
        "output-ancestor": workspace,
    }[relation]

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            overwrite=True,
            build_hooks_dir=build_hooks_dir,
            runtime_hooks_dir=runtime_hooks_dir,
        )

    assert raised.value.diagnostics[0].code == "render.input_output_overlap"
    assert hook.read_text() == "sentinel\n"


def test_build_and_runtime_hook_lock_namespaces_are_disjoint(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + """
[[comfyui.custom_nodes]]
type = "git"
url = "https://example.test/node.git"
ref = "1111111111111111111111111111111111111111"
pre_install_hooks = ["runtime-hooks/pre-start.d/10-pre.sh"]
"""
    )
    build_hooks = tmp_path / "build_hooks"
    build_hook = build_hooks / "runtime-hooks/pre-start.d/10-pre.sh"
    build_hook.parent.mkdir(parents=True)
    build_hook.write_text("build\n")
    build_hook.chmod(0o755)
    hooks = _runtime_hooks(tmp_path / "hooks")
    output = tmp_path / "context"

    _prepare(
        config,
        output,
        FakeAcquirer(),
        runtime_hooks_dir=hooks,
        build_hooks_dir=build_hooks,
    )

    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    identities = {
        canonical_entry_key(entry)
        for entry in lock.entries
        if isinstance(entry, LocalExecutableLockEntry)
    }
    build_identity = (
        "hooks",
        "build",
        "runtime-hooks/pre-start.d/10-pre.sh",
    )
    runtime_identity = ("hooks", "runtime", "pre-start.d/10-pre.sh")
    assert identities == {
        build_identity,
        runtime_identity,
        ("hooks", "runtime", "post-start.d/20-post.py"),
        ("hooks", "runtime", "stop.d/30-stop.sh"),
    }
    assert build_identity != runtime_identity
    assert (output / "build/hooks/runtime-hooks/pre-start.d/10-pre.sh").read_text() == (
        "build\n"
    )
    assert (output / "runtime/hooks/pre-start.d/10-pre.sh").read_text() == "pre\n"


# Hook-tree changes respect every no-write mode and closed filesystem validation.
def test_runtime_hook_change_add_delete_obey_all_no_write_modes(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = _runtime_hooks(tmp_path / "hooks")
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer(), runtime_hooks_dir=hooks)
    before = _tree(output)
    before_plan = (output / "build-plan.json").read_bytes()
    before_lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    before_request_digests = {
        entry.request_digest
        for entry in before_lock.entries
        if hasattr(entry, "request_digest")
    }

    changed = hooks / "pre-start.d/10-pre.sh"
    changed.write_text("changed\n")
    changed.chmod(0o755)
    locked_fake = FakeAcquirer()
    with pytest.raises(HostRenderServiceError) as locked:
        _prepare(
            config,
            output,
            locked_fake,
            runtime_hooks_dir=hooks,
            options=PlanningOptions(locked=True),
        )
    assert locked.value.diagnostics[0].code == "lock.locked_mismatch"
    assert locked_fake.calls == []
    check_fake = FakeAcquirer()
    with pytest.raises(HostRenderServiceError) as checked:
        _prepare(
            config,
            output,
            check_fake,
            runtime_hooks_dir=hooks,
            options=PlanningOptions(check=True),
        )
    assert checked.value.diagnostics[0].code == "render.context_changed"
    assert check_fake.calls == []
    dry_fake = FakeAcquirer()
    dry = _prepare(
        config,
        output,
        dry_fake,
        runtime_hooks_dir=hooks,
        options=PlanningOptions(dry_run=True),
    )
    assert dry.lock_result.changed
    assert dry_fake.calls == []
    assert _tree(output) == before

    added = hooks / "pre-start.d/11-added.py"
    added.write_text("print('added')\n")
    added.chmod(0o755)
    _prepare(config, output, FakeAcquirer(), runtime_hooks_dir=hooks, overwrite=True)
    updated_lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    assert {
        entry.request_digest
        for entry in updated_lock.entries
        if hasattr(entry, "request_digest")
    } == before_request_digests
    assert (output / "build-plan.json").read_bytes() != before_plan
    assert (output / "runtime/hooks/pre-start.d/11-added.py").is_file()

    added.unlink()
    with pytest.raises(HostRenderServiceError) as deleted_check:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            runtime_hooks_dir=hooks,
            options=PlanningOptions(check=True),
        )
    assert deleted_check.value.diagnostics[0].code == "render.context_changed"
    _prepare(config, output, FakeAcquirer(), runtime_hooks_dir=hooks, overwrite=True)
    assert not (output / "runtime/hooks/pre-start.d/11-added.py").exists()


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX FIFO")
def test_runtime_hook_tree_rejects_symlinks_and_special_files(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = _runtime_hooks(tmp_path / "hooks")
    target = hooks / "pre-start.d/10-pre.sh"
    target.unlink()
    target.symlink_to(tmp_path / "outside")
    with pytest.raises(HostRenderServiceError) as symlinked:
        _prepare(config, tmp_path / "context", FakeAcquirer(), runtime_hooks_dir=hooks)
    assert symlinked.value.diagnostics[0].code == "runtime_hooks.symlink"

    target.unlink()
    os.mkfifo(target)
    with pytest.raises(HostRenderServiceError) as special:
        _prepare(config, tmp_path / "context", FakeAcquirer(), runtime_hooks_dir=hooks)
    assert special.value.diagnostics[0].code == "runtime_hooks.special_file"


def test_runtime_hooks_require_an_explicit_source(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    _runtime_hooks(tmp_path / "hooks")

    prepared = _prepare(
        config,
        tmp_path / "context",
        FakeAcquirer(),
        working_directory=tmp_path,
    )

    assert prepared.plan.runtime.hooks == ()
    assert not (tmp_path / "context/runtime/hooks").exists()


def test_runtime_hook_tree_requires_existing_source(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = tmp_path / "hooks"
    fake = FakeAcquirer()

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, tmp_path / "context", fake, runtime_hooks_dir=hooks)

    assert raised.value.diagnostics[0].code == "runtime_hooks.source_not_directory"
    assert fake.calls == []


# Ordinary non-hook content stays outside image identity and materialization while
# the host retains one bounded warning for each affected tree location.
def test_runtime_hook_tree_ignores_unselected_content_with_aggregated_warnings(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "README.md").write_text("notes")
    (hooks / "examples").mkdir()
    phase = hooks / "pre-start.d"
    phase.mkdir()
    selected = phase / "10-hook.sh"
    selected.write_text("selected\n")
    (phase / "notes.txt").write_text("ignored")
    nested = phase / "nested"
    nested.mkdir()
    (nested / "20-not-discovered.sh").write_text("ignored")

    prepared = _prepare(
        config,
        tmp_path / "context",
        FakeAcquirer(),
        runtime_hooks_dir=hooks,
    )

    assert [hook.relative_path for hook in prepared.plan.runtime.hooks] == [
        "pre-start.d/10-hook.sh"
    ]
    assert len(prepared.warnings) == 2
    warnings = {(warning.path, warning.code): warning for warning in prepared.warnings}
    phase_warning = warnings[
        ("runtime_hooks_dir", "pre-start.d"),
        "runtime_hooks.ignored_phase_entries",
    ]
    root_warning = warnings[
        ("runtime_hooks_dir",),
        "runtime_hooks.ignored_top_level",
    ]
    assert "ignored 2 ordinary non-hook phase entries" in phase_warning.message
    assert "ignored 2 ordinary top-level" in root_warning.message
    context_hooks = tmp_path / "context" / "runtime" / "hooks"
    assert (context_hooks / "pre-start.d" / "10-hook.sh").is_file()
    assert not (context_hooks / "README.md").exists()
    assert not (context_hooks / "pre-start.d" / "notes.txt").exists()
    assert not (context_hooks / "pre-start.d" / "nested").exists()


def test_runtime_hook_tree_accepts_regular_0644_files(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = tmp_path / "hooks/pre-start.d"
    hooks.mkdir(parents=True)
    hook = hooks / "10-hook.sh"
    hook.write_text("hook\n")
    hook.chmod(0o644)

    prepared = _prepare(
        config, tmp_path / "context", FakeAcquirer(), runtime_hooks_dir=hooks.parent
    )

    assert prepared.plan.runtime.hooks[0].relative_path == "pre-start.d/10-hook.sh"
    assert (tmp_path / "context/runtime/hooks/pre-start.d/10-hook.sh").read_text() == (
        "hook\n"
    )
