"""Real subprocess coverage for deadline-owned runtime shutdown."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from comfyui_docker_helper.container import runtime_lifecycle as lifecycle_module
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookError,
    discover_runtime_hooks,
    run_runtime_stop_hooks,
)


# A real hook group that misses the immediate reap remains lifecycle-owned and
# is reaped concurrently after the absolute pre-stop deadline expires.
def test_real_long_stop_hook_is_killed_and_reaped_at_deadline(tmp_path: Path) -> None:
    runtime = ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )
    runtime.comfyui_path.mkdir(parents=True)
    hooks = tmp_path / "hooks"
    stop_dir = hooks / "stop.d"
    stop_dir.mkdir(parents=True)
    (stop_dir / "10-hang.sh").write_text(
        "trap '' TERM\nwhile :; do sleep 1; done\n",
        encoding="utf-8",
    )
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked-hooks",
        mounted_hooks_path=hooks,
    )
    started_at = time.monotonic()

    with pytest.raises(RuntimeHookError) as raised:
        run_runtime_stop_hooks(
            plan,
            runtime=runtime,
            deadline=started_at + 0.2,
            poll_interval_seconds=0.02,
        )

    assert time.monotonic() - started_at < 2.0
    assert [item.code for item in raised.value.diagnostics] == [
        "runtime_hook.shutdown_deadline"
    ]
    active_process = raised.value.active_process
    assert active_process is not None

    class CompletedChild:
        returncode: int | None = 0

        def poll(self) -> int | None:
            return self.returncode

        def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            pytest.fail("completed ComfyUI child must not be killed")

    class StoppedAuxiliary:
        def is_stopped(self) -> bool:
            return True

        def force_stop(self) -> bool:
            pytest.fail("stopped auxiliary must not be forced")

    now = time.monotonic()
    auxiliary = StoppedAuxiliary()
    assert (
        lifecycle_module._wait_for_managed_shutdown(
            CompletedChild(),  # type: ignore[arg-type]
            downloads=auxiliary,  # type: ignore[arg-type]
            ssh_service=auxiliary,  # type: ignore[arg-type]
            deadline=now + 1,
            auxiliary_deadline=now + 1,
            hook_processes=(active_process,),
            monotonic=time.monotonic,
            sleep=time.sleep,
        )
        == 0
    )
    assert active_process.poll() is not None
