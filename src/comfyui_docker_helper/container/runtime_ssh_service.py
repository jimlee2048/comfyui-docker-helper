"""Runtime SSH service lifetime ownership."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Protocol

from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container.process_control import wait_for_process_reap
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_diagnostics import runtime_error_reason
from comfyui_docker_helper.container.ssh import (
    SshdProcess,
    SshdStartupError,
    start_sshd_if_enabled,
)
from comfyui_docker_helper.errors import ApplicationError

SSHD_STOP_TIMEOUT_SECONDS = 5.0
SSHD_STOP_POLL_INTERVAL_SECONDS = 0.05


class RuntimeSshStarter(Protocol):
    """Start the configured runtime SSH service."""

    def __call__(
        self,
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Callable[[str], object],
    ) -> SshdProcess | None: ...


class RuntimeSshServiceError(ApplicationError):
    """The runtime SSH service failed at a lifecycle boundary."""


class RuntimeSshService:
    """Own sshd activation, health monitoring, and bounded shutdown."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        starter: RuntimeSshStarter = start_sshd_if_enabled,
        log: Callable[[str], object] = print,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._starter = starter
        self._log = log
        self._handle: SshdProcess | None = None
        self._shutdown_requested = threading.Event()

    def start(self) -> None:
        """Start sshd when enabled and effective credentials are available."""
        if not self._config.system.ssh.enable:
            return
        try:
            self._handle = self._starter(
                self._config,
                runtime=self._runtime,
                log=self._log,
            )
        except SshdStartupError as error:
            raise RuntimeSshServiceError(
                f"SSH runtime service failed to start: {error}"
            ) from error
        except ApplicationError as error:
            raise RuntimeSshServiceError(
                f"SSH runtime service failed to start: {error}"
            ) from error

    def ensure_running_before_comfyui(self) -> None:
        """Reject startup if an activated sshd has already exited."""
        if self._handle is None:
            return
        returncode = self._handle.poll()
        if returncode is not None:
            raise RuntimeSshServiceError(
                f"SSH runtime service exited before ComfyUI: {returncode}"
            )

    def monitor_after_comfyui_start(self) -> None:
        """Report unexpected sshd exit without changing ComfyUI ownership."""
        if self._handle is None:
            return

        def wait_for_exit() -> None:
            assert self._handle is not None
            try:
                returncode = self._handle.wait()
            except Exception as error:
                if self._shutdown_requested.is_set():
                    return
                print(
                    "WARNING: SSH runtime service monitor failed: "
                    f"reason={runtime_error_reason(error)}",
                    file=sys.stderr,
                )
                return
            if self._shutdown_requested.is_set():
                return
            print(
                "WARNING: SSH runtime service exited unexpectedly: "
                f"returncode={returncode}",
                file=sys.stderr,
            )

        threading.Thread(
            target=wait_for_exit,
            name="cdh-sshd-monitor",
            daemon=True,
        ).start()

    def stop(
        self,
        *,
        cancel_requested: Callable[[], bool],
        timeout: float = SSHD_STOP_TIMEOUT_SECONDS,
        poll_interval: float = SSHD_STOP_POLL_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = time.sleep,
    ) -> bool:
        """Terminate, bound, and reap the owned sshd child."""
        return stop_runtime_ssh_service(
            self._handle,
            cancel_requested=cancel_requested,
            shutdown_requested=self._shutdown_requested,
            timeout=timeout,
            poll_interval=poll_interval,
            monotonic=monotonic,
            sleep=sleep,
            log=self._log,
        )


def stop_runtime_ssh_service(
    handle: SshdProcess | None,
    *,
    cancel_requested: Callable[[], bool],
    shutdown_requested: threading.Event,
    timeout: float = SSHD_STOP_TIMEOUT_SECONDS,
    poll_interval: float = SSHD_STOP_POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    log: Callable[[str], object] = print,
) -> bool:
    """Terminate, bound, and reap one cdh-owned sshd child."""
    if handle is None or handle.poll() is not None:
        return True

    shutdown_requested.set()
    log("SSH runtime service stop requested")
    try:
        handle.terminate()
    except Exception as error:
        print(
            "WARNING: SSH runtime service terminate failed: "
            f"reason={runtime_error_reason(error)}",
            file=sys.stderr,
        )
        _kill_runtime_ssh_service(handle)
        wait_for_process_reap(
            handle,
            timeout=timeout,
            poll_interval=poll_interval,
            monotonic=monotonic,
            sleep=sleep,
        )
        return False

    deadline = monotonic() + timeout
    while handle.poll() is None:
        if cancel_requested():
            print(
                "WARNING: SSH runtime service stop interrupted; killing sshd",
                file=sys.stderr,
            )
            _kill_runtime_ssh_service(handle)
            wait_for_process_reap(
                handle,
                timeout=timeout,
                poll_interval=poll_interval,
                monotonic=monotonic,
                sleep=sleep,
            )
            return False
        now = monotonic()
        if now >= deadline:
            print(
                "WARNING: SSH runtime service did not stop in "
                f"{timeout:.1f}s; killing sshd",
                file=sys.stderr,
            )
            _kill_runtime_ssh_service(handle)
            wait_for_process_reap(
                handle,
                timeout=timeout,
                poll_interval=poll_interval,
                monotonic=monotonic,
                sleep=sleep,
            )
            return False
        sleep(min(poll_interval, deadline - now))
    wait_for_process_reap(
        handle,
        timeout=0.0,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
    )
    log("SSH runtime service stopped")
    return True


def _kill_runtime_ssh_service(handle: SshdProcess) -> None:
    try:
        handle.kill()
    except Exception as error:
        print(
            "WARNING: SSH runtime service kill failed: "
            f"reason={runtime_error_reason(error)}",
            file=sys.stderr,
        )
