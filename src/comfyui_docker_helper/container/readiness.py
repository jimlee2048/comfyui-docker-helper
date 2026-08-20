"""ComfyUI HTTP readiness probing for runtime startup."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from comfyui_docker_helper.config import Diagnostic

READINESS_HOST = "127.0.0.1"
READINESS_PATH = "/system_stats"
READINESS_TIMEOUT_SECONDS = 30.0
READINESS_POLL_INTERVAL_SECONDS = 0.5
READINESS_CONNECT_TIMEOUT_SECONDS = 1.0
READINESS_READ_TIMEOUT_SECONDS = 2.0


class ReadinessError(ValueError):
    """Readiness failure represented by stable diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("readiness errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("ComfyUI readiness failed")


class ReadinessChild(Protocol):
    """Child process state needed during readiness polling."""

    def poll(self) -> int | None: ...


class ReadinessResponse(Protocol):
    """Minimal HTTP response surface used by the readiness probe."""

    status_code: int

    def json(self) -> object: ...


class ReadinessHttpGet(Protocol):
    """Injectable HTTP GET callable for readiness probes."""

    def __call__(
        self,
        url: str,
        *,
        timeout: httpx.Timeout,
    ) -> ReadinessResponse: ...


type ReadinessProbe = Callable[[int], "ReadinessProbeResult"]
type Sleep = Callable[[float], object]
type Monotonic = Callable[[], float]


@dataclass(frozen=True, slots=True)
class ReadinessProbeResult:
    """One HTTP readiness probe result."""

    ready: bool
    reason: str = ""


def probe_comfyui_readiness(
    port: int,
    *,
    http_get: ReadinessHttpGet = httpx.get,
    connect_timeout: float = READINESS_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = READINESS_READ_TIMEOUT_SECONDS,
) -> ReadinessProbeResult:
    """Probe ComfyUI readiness through its loopback system_stats endpoint."""
    url = f"http://{READINESS_HOST}:{port}{READINESS_PATH}"
    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=read_timeout,
        pool=connect_timeout,
    )
    try:
        response = http_get(url, timeout=timeout)
    except httpx.HTTPError:
        return ReadinessProbeResult(
            ready=False,
            reason="readiness probe request failed",
        )

    if response.status_code != 200:
        return ReadinessProbeResult(
            ready=False,
            reason=f"readiness probe returned HTTP {response.status_code}",
        )

    try:
        payload = response.json()
    except ValueError:
        return ReadinessProbeResult(
            ready=False,
            reason="readiness probe returned invalid JSON",
        )

    if not isinstance(payload, dict):
        return ReadinessProbeResult(
            ready=False,
            reason="readiness probe JSON payload must be an object",
        )

    missing = [field for field in ("system", "devices") if field not in payload]
    if missing:
        names = ", ".join(missing)
        return ReadinessProbeResult(
            ready=False,
            reason=f"readiness probe JSON payload is missing: {names}",
        )

    return ReadinessProbeResult(ready=True)


def wait_for_comfyui_readiness(
    port: int,
    *,
    child: ReadinessChild,
    probe: ReadinessProbe = probe_comfyui_readiness,
    timeout_seconds: float = READINESS_TIMEOUT_SECONDS,
    poll_interval_seconds: float = READINESS_POLL_INTERVAL_SECONDS,
    monotonic: Monotonic = time.monotonic,
    sleep: Sleep = time.sleep,
) -> ReadinessProbeResult:
    """Wait until ComfyUI is ready or startup readiness fails."""
    if timeout_seconds <= 0:
        raise ValueError("readiness timeout must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("readiness poll interval must be positive")

    deadline = monotonic() + timeout_seconds
    while True:
        _raise_if_child_exited(child)
        result = probe(port)
        if result.ready:
            return result
        now = monotonic()
        if now >= deadline:
            raise ReadinessError(
                (
                    Diagnostic(
                        path=("readiness",),
                        code="readiness.timeout",
                        message="ComfyUI did not become ready before timeout",
                        hint=(
                            "Inspect ComfyUI startup output and verify its listen port"
                        ),
                    ),
                )
            )

        sleep(min(poll_interval_seconds, deadline - now))


def _raise_if_child_exited(child: ReadinessChild) -> None:
    returncode = child.poll()
    if returncode is None:
        return
    raise ReadinessError(
        (
            Diagnostic(
                path=("readiness",),
                code="readiness.child_exited",
                message=(
                    f"ComfyUI exited before readiness succeeded with code {returncode}"
                ),
            ),
        )
    )
