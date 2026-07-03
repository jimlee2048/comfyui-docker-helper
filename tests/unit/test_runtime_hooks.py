"""Tests for runtime lifecycle hook discovery and pre-start execution."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
)
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookError,
    discover_runtime_hooks,
    run_runtime_hooks,
)


def _runtime(tmp_path: Path) -> ContainerRuntime:
    return ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )


def _write_hook(root: Path, phase_dir: str, filename: str, content: str = "") -> Path:
    phase = root / phase_dir
    phase.mkdir(parents=True, exist_ok=True)
    path = phase / filename
    path.write_text(content or f"# {filename}\n", encoding="utf-8")
    return path


def test_discovery_order_is_baked_then_mounted_lexical_and_allows_duplicates(
    tmp_path: Path,
) -> None:
    baked = tmp_path / "baked"
    mounted = tmp_path / "mounted"
    _write_hook(baked, "pre-start.d", "20-second.sh")
    _write_hook(baked, "pre-start.d", "10-first.py")
    _write_hook(mounted, "pre-start.d", "10-first.py")
    _write_hook(mounted, "pre-start.d", "30-third.sh")

    plan = discover_runtime_hooks(
        baked_hooks_path=baked,
        mounted_hooks_path=mounted,
    )

    assert [
        (hook.source, hook.phase, hook.filename) for hook in plan.for_phase("pre-start")
    ] == [
        ("baked", "pre-start", "10-first.py"),
        ("baked", "pre-start", "20-second.sh"),
        ("mounted", "pre-start", "10-first.py"),
        ("mounted", "pre-start", "30-third.sh"),
    ]


def test_missing_roots_have_no_hooks(tmp_path: Path) -> None:
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=tmp_path / "missing-mounted",
    )

    assert plan.hooks == ()
    assert plan.for_phase("pre-start") == ()


def test_unknown_root_entries_and_future_phase_dirs_are_ignored(tmp_path: Path) -> None:
    baked = tmp_path / "baked"
    baked.mkdir()
    (baked / "README.md").write_text("ignored\n", encoding="utf-8")
    (baked / "future-start.d").mkdir()
    (baked / "future-start.d" / "bad.txt").write_text("ignored\n", encoding="utf-8")
    _write_hook(baked, "pre-start.d", "10-pre.sh")

    plan = discover_runtime_hooks(
        baked_hooks_path=baked,
        mounted_hooks_path=tmp_path / "missing-mounted",
    )

    assert [(hook.source, hook.phase, hook.filename) for hook in plan.hooks] == [
        ("baked", "pre-start", "10-pre.sh")
    ]


def test_strict_validation_checks_all_known_phase_dirs(tmp_path: Path) -> None:
    baked = tmp_path / "baked"
    mounted = tmp_path / "mounted"
    _write_hook(baked, "pre-start.d", "10-pre.sh")
    _write_hook(mounted, "pre-start.d", "10-pre.sh")
    _write_hook(mounted, "post-start.d", "notes.txt")
    (mounted / "stop.d" / "nested").mkdir(parents=True)

    with pytest.raises(RuntimeHookError) as error:
        discover_runtime_hooks(baked_hooks_path=baked, mounted_hooks_path=mounted)

    assert locations_and_codes(error.value) == [
        (
            ("hooks", "mounted", "post-start", "notes.txt"),
            "runtime_hook.unsupported_extension",
        ),
        (("hooks", "mounted", "stop", "nested"), "runtime_hook.directory"),
    ]


def test_strict_validation_rejects_symlinks(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    mounted = tmp_path / "mounted"
    phase = mounted / "pre-start.d"
    phase.mkdir(parents=True)
    real = tmp_path / "real.sh"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    (phase / "10-link.sh").symlink_to(real)

    with pytest.raises(RuntimeHookError) as error:
        discover_runtime_hooks(
            baked_hooks_path=tmp_path / "missing-baked",
            mounted_hooks_path=mounted,
        )

    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "pre-start", "10-link.sh"), "runtime_hook.symlink")
    ]


def test_strict_validation_rejects_special_files(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("fifo special files are not supported on this platform")
    mounted = tmp_path / "mounted"
    phase = mounted / "stop.d"
    phase.mkdir(parents=True)
    os.mkfifo(phase / "pipe.sh")

    with pytest.raises(RuntimeHookError) as error:
        discover_runtime_hooks(
            baked_hooks_path=tmp_path / "missing-baked",
            mounted_hooks_path=mounted,
        )

    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "stop", "pipe.sh"), "runtime_hook.special_file")
    ]


def test_run_pre_start_hooks_uses_suffix_mapping_env_cwd_and_logs(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    baked = tmp_path / "baked"
    _write_hook(baked, "pre-start.d", "10-shell.sh")
    _write_hook(baked, "pre-start.d", "20-python.py")
    plan = discover_runtime_hooks(
        baked_hooks_path=baked,
        mounted_hooks_path=tmp_path / "missing-mounted",
    )
    calls: list[tuple[list[str], Path, dict[str, str], str]] = []
    logs: list[str] = []

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
    ) -> object:
        calls.append(
            (
                [os.fspath(argument) for argument in argv],
                Path(cwd),
                dict(env),
                description,
            )
        )
        return object()

    results = run_runtime_hooks(
        plan,
        "pre-start",
        runtime=runtime,
        env={"PATH": "/usr/bin", "EXTRA": "1"},
        log=logs.append,
        runner=runner,
    )

    assert [result.status for result in results] == ["completed", "completed"]
    assert calls == [
        (
            ["bash", str(baked / "pre-start.d" / "10-shell.sh")],
            runtime.comfyui_path,
            {
                "PATH": f"{runtime.virtual_env / 'bin'}:/usr/bin",
                "EXTRA": "1",
                "WORKSPACE": str(runtime.workspace),
                "COMFYUI_PATH": str(runtime.comfyui_path),
                "VIRTUAL_ENV": str(runtime.virtual_env),
            },
            "runtime hook baked/pre-start/10-shell.sh",
        ),
        (
            [str(runtime.python), str(baked / "pre-start.d" / "20-python.py")],
            runtime.comfyui_path,
            {
                "PATH": f"{runtime.virtual_env / 'bin'}:/usr/bin",
                "EXTRA": "1",
                "WORKSPACE": str(runtime.workspace),
                "COMFYUI_PATH": str(runtime.comfyui_path),
                "VIRTUAL_ENV": str(runtime.virtual_env),
            },
            "runtime hook baked/pre-start/20-python.py",
        ),
    ]
    assert logs == [
        "Running runtime hook source=baked phase=pre-start filename=10-shell.sh",
        "Running runtime hook source=baked phase=pre-start filename=20-python.py",
    ]


def test_pre_start_hook_failure_stops_phase(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    baked = tmp_path / "baked"
    _write_hook(baked, "pre-start.d", "10-ok.sh")
    _write_hook(baked, "pre-start.d", "20-fail.sh")
    _write_hook(baked, "pre-start.d", "30-skip.sh")
    plan = discover_runtime_hooks(
        baked_hooks_path=baked,
        mounted_hooks_path=tmp_path / "missing-mounted",
    )
    calls: list[str] = []

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
    ) -> object:
        del cwd, env, description
        filename = Path(argv[-1]).name
        calls.append(filename)
        if filename == "20-fail.sh":
            raise ContainerCommandError("hook failed", exit_code=12)
        return object()

    with pytest.raises(RuntimeHookError) as error:
        run_runtime_hooks(
            plan,
            "pre-start",
            runtime=runtime,
            env={},
            runner=runner,
        )

    assert calls == ["10-ok.sh", "20-fail.sh"]
    assert locations_and_codes(error.value) == [
        (("hooks", "baked", "pre-start", "20-fail.sh"), "runtime_hook.execution_failed")
    ]


def locations_and_codes(
    error: RuntimeHookError,
) -> list[tuple[tuple[object, ...], str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]
