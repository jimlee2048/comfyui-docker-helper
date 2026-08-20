"""Presentation-neutral detail and terminal capability policy."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Protocol

_UNICODE_PROBE = "└"


class OutputDetail(IntEnum):
    """Maximum optional cdh-owned detail admitted for one invocation."""

    QUIET = 0
    NORMAL = 1
    VERBOSE = 2
    DEBUG = 3


@dataclass(frozen=True, slots=True)
class CliOutputSettings:
    """Root CLI output settings inherited by one command invocation."""

    detail: OutputDetail = OutputDetail.NORMAL

    @classmethod
    def from_cli_options(
        cls,
        *,
        quiet: bool,
        verbosity: int,
    ) -> CliOutputSettings:
        """Resolve accepted root quiet and verbosity options."""
        if quiet and verbosity:
            raise ValueError("quiet and verbose output are mutually exclusive")
        if quiet:
            detail = OutputDetail.QUIET
        elif verbosity <= 0:
            detail = OutputDetail.NORMAL
        elif verbosity == 1:
            detail = OutputDetail.VERBOSE
        else:
            detail = OutputDetail.DEBUG
        return cls(detail=detail)

    def includes(self, minimum: OutputDetail) -> bool:
        """Return whether optional output at ``minimum`` is admitted."""
        return self.detail >= minimum


class OutputContextKind(Enum):
    """Execution context that constrains presentation behavior."""

    ONE_SHOT = "one-shot"
    DURABLE = "durable"


class OutputStream(Enum):
    """Cdh-owned output destination."""

    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class StreamCapabilities:
    """Terminal facts for one concrete output destination."""

    is_terminal: bool
    supports_live: bool
    supports_color: bool
    supports_unicode: bool

    def __post_init__(self) -> None:
        if not self.is_terminal and (
            self.supports_live or self.supports_color or self.supports_unicode
        ):
            raise ValueError("A non-terminal destination must use plain output.")

    @classmethod
    def from_facts(
        cls,
        *,
        is_terminal: bool,
        no_color: bool,
        term: str | None,
        encoding: str | None,
    ) -> StreamCapabilities:
        """Derive conservative capabilities from boundary-owned stream facts."""
        capable_terminal = is_terminal and (term or "").lower() not in {
            "dumb",
            "unknown",
        }
        return cls(
            is_terminal=is_terminal,
            supports_live=capable_terminal,
            supports_color=capable_terminal and not no_color,
            supports_unicode=capable_terminal and _supports_unicode(encoding),
        )


class TerminalStream(Protocol):
    """Narrow stream surface needed for capability detection."""

    encoding: str | None

    def isatty(self) -> bool:
        """Return whether the destination is an attached terminal."""


def detect_stream_capabilities(
    stream: TerminalStream,
    *,
    environment: Mapping[str, str] | None = None,
) -> StreamCapabilities:
    """Inspect one real destination without allowing environment upgrades."""
    environment = os.environ if environment is None else environment
    try:
        is_terminal = bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        is_terminal = False
    if environment.get("TTY_COMPATIBLE") == "0":
        is_terminal = False
    interactive = is_terminal and environment.get("TTY_INTERACTIVE") != "0"
    capabilities = StreamCapabilities.from_facts(
        is_terminal=is_terminal,
        no_color=environment.get("NO_COLOR", "") != "",
        term=environment.get("TERM"),
        encoding=getattr(stream, "encoding", None),
    )
    if interactive:
        return capabilities
    return StreamCapabilities(
        is_terminal=capabilities.is_terminal,
        supports_live=False,
        supports_color=capabilities.supports_color,
        supports_unicode=capabilities.supports_unicode,
    )


@dataclass(frozen=True, slots=True)
class OutputPolicy:
    """Combine invocation settings with independent destination capabilities."""

    settings: CliOutputSettings
    stdout: StreamCapabilities
    stderr: StreamCapabilities
    context: OutputContextKind

    def capabilities(self, stream: OutputStream) -> StreamCapabilities:
        """Return the capability facts for ``stream``."""
        if stream is OutputStream.STDOUT:
            return self.stdout
        return self.stderr

    def includes(self, minimum: OutputDetail) -> bool:
        """Return whether optional output at ``minimum`` is admitted."""
        return self.settings.includes(minimum)

    def allows_live(self, stream: OutputStream) -> bool:
        """Return whether cdh may use cursor-rewriting presentation."""
        return (
            self.context is OutputContextKind.ONE_SHOT
            and self.capabilities(stream).is_terminal
            and self.capabilities(stream).supports_live
        )


def _supports_unicode(encoding: str | None) -> bool:
    if encoding is None:
        return False
    try:
        _UNICODE_PROBE.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True
