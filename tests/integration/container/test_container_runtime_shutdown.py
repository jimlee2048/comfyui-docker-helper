"""Real subprocess coverage for deadline-owned runtime shutdown."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

import pytest

from comfyui_docker_helper.container.process_control import (
    reap_process_if_exited,
    send_direct_process_signal,
    signal_process_group,
    terminate_direct_process,
    wait_for_process_reap,
)
from comfyui_docker_helper.container.runners import ContainerRuntime, start_argv
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookError,
    discover_runtime_hooks,
    run_runtime_stop_hooks,
)
from comfyui_docker_helper.container.runtime_lifecycle import (
    _wait_for_managed_shutdown,
)
from tests.runtime_event_support import RecordingRuntimeEventSink

_PROCESS_CLEANUP_TIMEOUT_SECONDS = 10
_PROCESS_CLEANUP_POLL_INTERVAL_SECONDS = 0.02


class _OwnedRuntimeProcesses:
    def __init__(self) -> None:
        self.direct_processes: list[subprocess.Popen[bytes]] = []
        self.process_groups: list[tuple[int, subprocess.Popen[bytes]]] = []

    def register_direct(
        self, process: subprocess.Popen[bytes]
    ) -> subprocess.Popen[bytes]:
        self.direct_processes.append(process)
        return process

    def start_hook(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
        start_new_session: bool = False,
    ) -> subprocess.Popen[bytes]:
        process = start_argv(
            argv,
            cwd=cwd,
            env=env,
            description=description,
            start_new_session=start_new_session,
        )
        if start_new_session:
            self.process_groups.append((process.pid, process))
        else:
            self.direct_processes.append(process)
        return process


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait_for_process_group_exit(
    process_group_id: int,
    leader: subprocess.Popen[bytes],
    *,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        with suppress(OSError):
            reap_process_if_exited(leader)
        if not _process_group_exists(process_group_id):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_PROCESS_CLEANUP_POLL_INTERVAL_SECONDS, remaining))


def _cleanup_owned_process_group(
    process_group_id: int,
    leader: subprocess.Popen[bytes],
) -> bool:
    group_exited = not _process_group_exists(process_group_id)
    if not group_exited:
        with suppress(OSError):
            signal_process_group(process_group_id, signal.SIGTERM)
        group_exited = _wait_for_process_group_exit(
            process_group_id,
            leader,
            timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
        )
    if not group_exited:
        with suppress(OSError):
            signal_process_group(process_group_id, signal.SIGKILL)
        group_exited = _wait_for_process_group_exit(
            process_group_id,
            leader,
            timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
        )
    try:
        leader_reaped = wait_for_process_reap(
            leader,
            timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
            poll_interval=_PROCESS_CLEANUP_POLL_INTERVAL_SECONDS,
        )
    except OSError:
        leader_reaped = False
    return group_exited and leader_reaped


@pytest.fixture
def owned_runtime_processes(
    request: pytest.FixtureRequest,
) -> _OwnedRuntimeProcesses:
    processes = _OwnedRuntimeProcesses()

    def cleanup_processes() -> None:
        cleanup_succeeded = True
        for process in reversed(processes.direct_processes):
            try:
                cleanup_succeeded = (
                    terminate_direct_process(
                        process,
                        terminate_timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
                        kill_timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
                        poll_interval=_PROCESS_CLEANUP_POLL_INTERVAL_SECONDS,
                    )
                    and cleanup_succeeded
                )
            except OSError:
                cleanup_succeeded = False
        for process_group_id, leader in reversed(processes.process_groups):
            try:
                cleanup_succeeded = (
                    _cleanup_owned_process_group(process_group_id, leader)
                    and cleanup_succeeded
                )
            except OSError:
                cleanup_succeeded = False
        if not cleanup_succeeded:
            pytest.fail(
                "test-owned runtime process remained after bounded cleanup",
                pytrace=False,
            )

    request.addfinalizer(cleanup_processes)
    return processes


# A stop-hook cutoff transfers the exact killed-but-unreaped group leader to the
# lifecycle final window, where it is reaped alongside the signaled main child.
def test_real_long_stop_hook_is_reaped_in_lifecycle_final_window(
    tmp_path: Path,
    owned_runtime_processes: _OwnedRuntimeProcesses,
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
            event_sink=RecordingRuntimeEventSink(),
            deadline=started_at + 0.2,
            poll_interval_seconds=0.02,
            runner=owned_runtime_processes.start_hook,
        )

    hook_process = raised.value.active_process
    assert hook_process is not None
    assert time.monotonic() - started_at < 2.0
    assert [item.code for item in raised.value.diagnostics] == [
        "runtime_hook.shutdown_deadline"
    ]

    child = owned_runtime_processes.register_direct(
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            shell=False,
        )
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
