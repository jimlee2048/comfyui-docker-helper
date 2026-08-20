"""Tests for ComfyUI runtime readiness probing."""

from __future__ import annotations

import httpx
import pytest

from comfyui_docker_helper.container.readiness import (
    READINESS_CONNECT_TIMEOUT_SECONDS,
    READINESS_HOST,
    READINESS_PATH,
    READINESS_POLL_INTERVAL_SECONDS,
    READINESS_READ_TIMEOUT_SECONDS,
    READINESS_TIMEOUT_SECONDS,
    ReadinessError,
    ReadinessProbeResult,
    probe_comfyui_readiness,
    wait_for_comfyui_readiness,
)


class FakeClock:
    """Manual monotonic clock for fast readiness timeout tests."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RunningChild:
    """Child process that stays alive during readiness polling."""

    def poll(self) -> int | None:
        return None


class ExitedChild:
    """Child process that has already exited."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


class FakeResponse:
    """Minimal response object for readiness probe tests."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: object | None = None,
        json_error: ValueError | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload = {"system": {}, "devices": []} if payload is None else payload
        self.json_error = json_error

    def json(self) -> object:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


# Readiness polling is bounded, loopback-only, and coupled to child liveness.
def test_readiness_constants_are_bounded() -> None:
    assert READINESS_HOST == "127.0.0.1"
    assert READINESS_PATH == "/system_stats"
    assert 0 < READINESS_POLL_INTERVAL_SECONDS < READINESS_TIMEOUT_SECONDS
    assert 0 < READINESS_CONNECT_TIMEOUT_SECONDS < READINESS_TIMEOUT_SECONDS
    assert 0 < READINESS_READ_TIMEOUT_SECONDS < READINESS_TIMEOUT_SECONDS


def test_probe_uses_loopback_effective_port_and_timeout_constants() -> None:
    calls: list[tuple[str, httpx.Timeout]] = []

    def http_get(url: str, *, timeout: httpx.Timeout) -> FakeResponse:
        calls.append((url, timeout))
        return FakeResponse()

    result = probe_comfyui_readiness(8299, http_get=http_get)

    assert result.ready is True
    assert calls[0][0] == "http://127.0.0.1:8299/system_stats"
    assert calls[0][1].connect == READINESS_CONNECT_TIMEOUT_SECONDS
    assert calls[0][1].read == READINESS_READ_TIMEOUT_SECONDS


def test_wait_succeeds_after_failed_polls() -> None:
    clock = FakeClock()
    attempts: list[int] = []

    def probe(port: int) -> ReadinessProbeResult:
        assert port == 8188
        attempts.append(port)
        if len(attempts) < 3:
            return ReadinessProbeResult(False, "not yet")
        return ReadinessProbeResult(True)

    result = wait_for_comfyui_readiness(
        8188,
        child=RunningChild(),
        probe=probe,
        timeout_seconds=1.0,
        poll_interval_seconds=0.1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result.ready is True
    assert len(attempts) == 3
    assert clock.sleeps == [0.1, 0.1]


def test_wait_times_out_when_probe_never_becomes_ready() -> None:
    clock = FakeClock()

    with pytest.raises(ReadinessError) as error:
        wait_for_comfyui_readiness(
            8188,
            child=RunningChild(),
            probe=lambda port: ReadinessProbeResult(False, f"port {port} not ready"),
            timeout_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert locations_and_codes(error.value) == [(("readiness",), "readiness.timeout")]
    diagnostic = error.value.diagnostics[0]
    assert diagnostic.message == "ComfyUI did not become ready before timeout"
    assert diagnostic.hint == (
        "Inspect ComfyUI startup output and verify its listen port"
    )
    assert "port 8188 not ready" not in diagnostic.message


@pytest.mark.parametrize(
    ("response_factory", "expected_reason"),
    [
        (
            lambda: FakeResponse(status_code=503),
            "readiness probe returned HTTP 503",
        ),
        (
            lambda: FakeResponse(json_error=ValueError("bad json")),
            "readiness probe returned invalid JSON",
        ),
        (
            lambda: FakeResponse(payload={"system": {}}),
            "readiness probe JSON payload is missing: devices",
        ),
        (
            lambda: FakeResponse(payload=[]),
            "readiness probe JSON payload must be an object",
        ),
    ],
)
def test_probe_not_ready_responses_eventually_fail_readiness(
    response_factory,
    expected_reason: str,
) -> None:
    clock = FakeClock()

    def http_get(url: str, *, timeout: httpx.Timeout) -> FakeResponse:
        del url, timeout
        return response_factory()

    probe_result = probe_comfyui_readiness(8188, http_get=http_get)
    assert probe_result.reason == expected_reason

    with pytest.raises(ReadinessError) as error:
        wait_for_comfyui_readiness(
            8188,
            child=RunningChild(),
            probe=lambda port: probe_comfyui_readiness(port, http_get=http_get),
            timeout_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert locations_and_codes(error.value) == [(("readiness",), "readiness.timeout")]
    diagnostic = error.value.diagnostics[0]
    assert diagnostic.message == "ComfyUI did not become ready before timeout"
    assert expected_reason not in diagnostic.message


def test_transport_errors_eventually_fail_readiness() -> None:
    clock = FakeClock()

    def http_get(url: str, *, timeout: httpx.Timeout) -> FakeResponse:
        del timeout
        request = httpx.Request("GET", url)
        raise httpx.ConnectError("connection refused", request=request)

    probe_result = probe_comfyui_readiness(8188, http_get=http_get)
    assert probe_result.reason == "readiness probe request failed"

    with pytest.raises(ReadinessError) as error:
        wait_for_comfyui_readiness(
            8188,
            child=RunningChild(),
            probe=lambda port: probe_comfyui_readiness(port, http_get=http_get),
            timeout_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert "connection refused" not in error.value.diagnostics[0].message
    assert error.value.diagnostics[0].code == "readiness.timeout"


@pytest.mark.parametrize("returncode", [0, 9])
def test_child_exit_before_ready_fails_startup(returncode: int) -> None:
    probe_calls: list[int] = []

    def probe(port: int) -> ReadinessProbeResult:
        probe_calls.append(port)
        return ReadinessProbeResult(True)

    with pytest.raises(ReadinessError) as error:
        wait_for_comfyui_readiness(
            8188,
            child=ExitedChild(returncode),
            probe=probe,
        )

    assert probe_calls == []
    assert locations_and_codes(error.value) == [
        (("readiness",), "readiness.child_exited")
    ]
    assert f"code {returncode}" in error.value.diagnostics[0].message


def locations_and_codes(
    error: ReadinessError,
) -> list[tuple[tuple[object, ...], str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]
