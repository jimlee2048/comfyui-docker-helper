"""Real subprocess coverage for deadline-owned runtime shutdown."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from comfyui_docker_helper.container.process_control import send_direct_process_signal
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookError,
    discover_runtime_hooks,
    run_runtime_stop_hooks,
)
from comfyui_docker_helper.container.runtime_lifecycle import (
    _wait_for_managed_shutdown,
)


# A stop-hook cutoff transfers the exact killed-but-unreaped group leader to the
# lifecycle final window, where it is reaped alongside the signaled main child.
def test_real_long_stop_hook_is_reaped_in_lifecycle_final_window(
    tmp_path: Path,
) -> None:
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
    hook_process = raised.value.active_process
    assert hook_process is not None

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        shell=False,
    )
    send_direct_process_signal(child, signal.SIGTERM)

    class StoppedOwner:
        def is_stopped(self) -> bool:
            return True

    owner = StoppedOwner()
    final_deadline = time.monotonic() + 1.0
    result = _wait_for_managed_shutdown(
        child,
        downloads=owner,  # type: ignore[arg-type]
        ssh_service=owner,  # type: ignore[arg-type]
        deadline=final_deadline,
        auxiliary_deadline=final_deadline,
        hook_processes=(hook_process,),
        monotonic=time.monotonic,
        sleep=time.sleep,
    )

    assert result == -signal.SIGTERM
    assert child.returncode == -signal.SIGTERM
    assert hook_process.returncode == -signal.SIGKILL
