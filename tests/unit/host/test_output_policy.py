"""Shared CLI output detail and terminal capability policy."""

from dataclasses import FrozenInstanceError

import pytest

from comfyui_docker_helper.cli_output.policy import (
    CliOutputSettings,
    OutputContextKind,
    OutputDetail,
    OutputPolicy,
    OutputStream,
    StreamCapabilities,
    detect_stream_capabilities,
)
from comfyui_docker_helper.cli_output.text import control_safe_text


def _capabilities(
    *,
    terminal: bool,
    no_color: bool = False,
    term: str = "xterm-256color",
    encoding: str | None = "utf-8",
) -> StreamCapabilities:
    return StreamCapabilities.from_facts(
        is_terminal=terminal,
        no_color=no_color,
        term=term,
        encoding=encoding,
    )


@pytest.mark.parametrize(
    ("detail", "included"),
    [
        (OutputDetail.QUIET, ()),
        (OutputDetail.NORMAL, (OutputDetail.NORMAL,)),
        (
            OutputDetail.VERBOSE,
            (OutputDetail.NORMAL, OutputDetail.VERBOSE),
        ),
        (
            OutputDetail.DEBUG,
            (OutputDetail.NORMAL, OutputDetail.VERBOSE, OutputDetail.DEBUG),
        ),
    ],
)
def test_detail_policy_admits_only_requested_optional_information(
    detail: OutputDetail,
    included: tuple[OutputDetail, ...],
) -> None:
    settings = CliOutputSettings(detail=detail)

    assert (
        tuple(
            candidate
            for candidate in (
                OutputDetail.NORMAL,
                OutputDetail.VERBOSE,
                OutputDetail.DEBUG,
            )
            if settings.includes(candidate)
        )
        == included
    )
    assert settings.includes(OutputDetail.QUIET) is True


@pytest.mark.parametrize(
    ("quiet", "verbosity", "expected"),
    [
        (False, 0, OutputDetail.NORMAL),
        (True, 0, OutputDetail.QUIET),
        (False, 1, OutputDetail.VERBOSE),
        (False, 2, OutputDetail.DEBUG),
        (False, 3, OutputDetail.DEBUG),
    ],
)
def test_cli_options_map_to_bounded_detail_levels(
    quiet: bool,
    verbosity: int,
    expected: OutputDetail,
) -> None:
    settings = CliOutputSettings.from_cli_options(
        quiet=quiet,
        verbosity=verbosity,
    )

    assert settings.detail is expected


def test_quiet_and_verbose_cli_settings_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        CliOutputSettings.from_cli_options(quiet=True, verbosity=1)


def test_settings_and_capabilities_are_immutable() -> None:
    settings = CliOutputSettings()
    capabilities = _capabilities(terminal=True)

    with pytest.raises(FrozenInstanceError):
        settings.detail = OutputDetail.DEBUG  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        capabilities.supports_live = False  # type: ignore[misc]


def test_non_terminal_and_weak_terminal_capabilities_are_plain() -> None:
    redirected = _capabilities(terminal=False)
    weak_terminal = _capabilities(terminal=True, term="dumb")
    unknown_terminal = _capabilities(terminal=True, term="unknown")

    for capabilities in (redirected, weak_terminal, unknown_terminal):
        assert capabilities.supports_live is False
        assert capabilities.supports_color is False
        assert capabilities.supports_unicode is False


def test_non_terminal_capability_cannot_admit_interactive_features() -> None:
    with pytest.raises(ValueError, match="must use plain output"):
        StreamCapabilities(
            is_terminal=False,
            supports_live=True,
            supports_color=False,
            supports_unicode=False,
        )


def test_no_color_preserves_live_and_unicode_capabilities() -> None:
    capabilities = _capabilities(terminal=True, no_color=True)

    assert capabilities.supports_live is True
    assert capabilities.supports_color is False
    assert capabilities.supports_unicode is True


def test_unknown_or_incapable_encoding_disables_unicode_decoration() -> None:
    missing = _capabilities(terminal=True, encoding=None)
    ascii_only = _capabilities(terminal=True, encoding="ascii")

    assert missing.supports_unicode is False
    assert ascii_only.supports_unicode is False


def test_stdout_and_stderr_capabilities_are_independent() -> None:
    policy = OutputPolicy(
        settings=CliOutputSettings(),
        stdout=_capabilities(terminal=False),
        stderr=_capabilities(terminal=True),
        context=OutputContextKind.ONE_SHOT,
    )

    assert policy.allows_live(OutputStream.STDOUT) is False
    assert policy.allows_live(OutputStream.STDERR) is True


def test_durable_contexts_never_allow_live_output() -> None:
    policy = OutputPolicy(
        settings=CliOutputSettings(),
        stdout=_capabilities(terminal=True),
        stderr=_capabilities(terminal=True),
        context=OutputContextKind.DURABLE,
    )

    assert policy.allows_live(OutputStream.STDOUT) is False
    assert policy.allows_live(OutputStream.STDERR) is False


class _FakeStream:
    def __init__(
        self,
        *,
        terminal: bool,
        encoding: str | None = "utf-8",
        error: Exception | None = None,
    ) -> None:
        self.encoding = encoding
        self._terminal = terminal
        self._error = error

    def isatty(self) -> bool:
        if self._error is not None:
            raise self._error
        return self._terminal


def test_stream_detection_accepts_only_environment_downgrades() -> None:
    redirected = detect_stream_capabilities(
        _FakeStream(terminal=False),
        environment={"TTY_COMPATIBLE": "1", "TTY_INTERACTIVE": "1"},
    )
    downgraded = detect_stream_capabilities(
        _FakeStream(terminal=True),
        environment={"TTY_INTERACTIVE": "0"},
    )

    assert redirected.is_terminal is False
    assert redirected.supports_live is False
    assert downgraded.is_terminal is True
    assert downgraded.supports_live is False


def test_stream_detection_treats_failed_isatty_as_plain() -> None:
    capabilities = detect_stream_capabilities(
        _FakeStream(terminal=True, error=ValueError("closed")),
        environment={},
    )

    assert capabilities.is_terminal is False
    assert capabilities.supports_live is False


def test_control_safe_text_escapes_terminal_controls_and_backslashes() -> None:
    value = "label\\segment\nnext\r\t\x00\x1b\u2028\U0001f600"

    assert control_safe_text(value) == (
        "label\\\\segment\\nnext\\r\\t\\x00\\x1b\\u2028😀"
    )
