"""Plain Container helper-event presentation."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Literal, Protocol, TextIO

from comfyui_docker_helper.cli_output.events import EventSink
from comfyui_docker_helper.cli_output.policy import CliOutputSettings, OutputDetail
from comfyui_docker_helper.cli_output.text import control_safe_text
from comfyui_docker_helper.container.build.events import (
    ComfyUIInstallCompleted,
    ContainerHelperEvent,
    ContainerHelperPhase,
    ContainerHelperPhaseCompleted,
    ContainerHelperPhaseStarted,
    CustomNodeCompleted,
    CustomNodesInstallCompleted,
    FinalManifestCompleted,
    GitCustomNodeStarted,
    RegistryCustomNodeStarted,
)

_HELPER_PHASE_LABELS = {
    ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT: "Checking out ComfyUI source",
    ContainerHelperPhase.COMFYUI_SOURCE_VERIFICATION: "Verifying ComfyUI source",
    ContainerHelperPhase.PYTORCH_INSTALLATION: "Installing PyTorch packages",
    ContainerHelperPhase.PYTHON_EXTRAS_INSTALLATION: "Installing Python extras",
    ContainerHelperPhase.COMFYUI_REQUIREMENTS_INSTALLATION: (
        "Installing ComfyUI requirements"
    ),
    ContainerHelperPhase.MANAGER_INSTALLATION: "Installing ComfyUI-Manager",
    ContainerHelperPhase.COMFYUI_FINAL_VERIFICATION: (
        "Verifying the ComfyUI installation"
    ),
    ContainerHelperPhase.CUSTOM_NODES_PREPARATION: "Preparing custom-node installation",
    ContainerHelperPhase.CUSTOM_NODE_PRE_CLONE: "Running pre-clone hooks",
    ContainerHelperPhase.CUSTOM_NODE_SOURCE_PREPARATION: "Preparing custom-node source",
    ContainerHelperPhase.CUSTOM_NODE_PRE_INSTALL: "Running pre-install hooks",
    ContainerHelperPhase.CUSTOM_NODE_INSTALLATION: "Installing the custom node",
    ContainerHelperPhase.CUSTOM_NODE_POST_INSTALL: "Running post-install hooks",
    ContainerHelperPhase.CUSTOM_NODES_FINAL_VERIFICATION: (
        "Verifying custom-node installation"
    ),
    ContainerHelperPhase.FINAL_STATE_VERIFICATION: "Verifying final image state",
    ContainerHelperPhase.FINAL_MANIFEST_WRITE: "Writing the final manifest",
}


class _DetailFilter(Protocol):
    def includes(self, minimum: OutputDetail) -> bool: ...


class _PlainEventWriter:
    """Write one control-safe, flushed plain event line."""

    def __init__(
        self,
        *,
        stderr: TextIO,
        detail: _DetailFilter,
    ) -> None:
        self._stderr = stderr
        self._detail = detail

    def write(
        self,
        value: str,
        *,
        minimum: OutputDetail = OutputDetail.NORMAL,
    ) -> None:
        if not self._detail.includes(minimum):
            return
        self._stderr.write(f"{control_safe_text(value)}\n")
        self._stderr.flush()


class ContainerHelperDisplay(EventSink[ContainerHelperEvent]):
    """Render serial one-shot helper events as durable plain stderr lines."""

    def __init__(
        self,
        *,
        stderr: TextIO,
        settings: CliOutputSettings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._writer = _PlainEventWriter(stderr=stderr, detail=settings)
        self._clock = clock
        self._started_at = clock()
        self._active_phase: ContainerHelperPhase | None = None
        self._phase_started_at: float | None = None

    def emit(self, event: ContainerHelperEvent, /) -> None:
        """Render one helper event and validate only serial phase pairing."""
        if isinstance(event, ContainerHelperPhaseStarted):
            self._start_phase(event)
        elif isinstance(event, ContainerHelperPhaseCompleted):
            self._complete_phase(event)
        elif isinstance(event, RegistryCustomNodeStarted):
            self._registry_node(event)
        elif isinstance(event, GitCustomNodeStarted):
            self._git_node(event)
        elif isinstance(event, CustomNodeCompleted):
            self._writer.write(f"[{event.index}/{event.total}] Custom node complete")
        elif isinstance(event, ComfyUIInstallCompleted):
            self._write_terminal("ComfyUI installation complete")
        elif isinstance(event, CustomNodesInstallCompleted):
            value = "Custom-node installation complete"
            if event.node_count == 0 or self._settings.includes(OutputDetail.VERBOSE):
                noun = "node" if event.node_count == 1 else "nodes"
                value += f": {event.node_count} {noun}"
            self._write_terminal(value)
        elif isinstance(event, FinalManifestCompleted):
            self._write_terminal("Final manifest complete")
        else:
            raise TypeError("unsupported Container helper event")

    def _start_phase(self, event: ContainerHelperPhaseStarted) -> None:
        if self._active_phase is not None:
            raise ValueError("A helper phase cannot start while another is active.")
        self._active_phase = event.phase
        self._phase_started_at = self._clock()
        self._writer.write(_HELPER_PHASE_LABELS[event.phase])

    def _complete_phase(self, event: ContainerHelperPhaseCompleted) -> None:
        if self._active_phase is not event.phase:
            raise ValueError("Helper phase completion does not match its start.")
        phase_started_at = self._phase_started_at
        if phase_started_at is None:
            raise RuntimeError("An active helper phase must retain its start time.")
        duration = max(0.0, self._clock() - phase_started_at)
        self._active_phase = None
        self._phase_started_at = None
        self._writer.write(
            f"  Phase complete: {_HELPER_PHASE_LABELS[event.phase]} "
            f"in {_format_duration(duration)}",
            minimum=OutputDetail.VERBOSE,
        )

    def _registry_node(self, event: RegistryCustomNodeStarted) -> None:
        value = f"[{event.index}/{event.total}] Custom node: {event.id} {event.version}"
        self._writer.write(self._node_detail(value, event, source="registry"))

    def _git_node(self, event: GitCustomNodeStarted) -> None:
        value = f"[{event.index}/{event.total}] Custom node: {event.target_name}"
        self._writer.write(self._node_detail(value, event, source="git"))

    def _node_detail(
        self,
        value: str,
        event: RegistryCustomNodeStarted | GitCustomNodeStarted,
        *,
        source: Literal["registry", "git"],
    ) -> str:
        if self._settings.includes(OutputDetail.VERBOSE):
            value += " ("
            if isinstance(event, GitCustomNodeStarted):
                value += f"pre-clone hooks={event.pre_clone_hook_count}, "
            value += (
                f"pre-install hooks={event.pre_hook_count}, "
                f"post-install hooks={event.post_hook_count}"
            )
            if self._settings.includes(OutputDetail.DEBUG):
                value += f", source={source}"
            value += ")"
        return value

    def _write_terminal(self, value: str) -> None:
        if self._settings.includes(OutputDetail.VERBOSE):
            duration = max(0.0, self._clock() - self._started_at)
            value += f" in {_format_duration(duration)}"
        self._writer.write(value)


def default_container_helper_display(
    settings: CliOutputSettings,
    *,
    stderr: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ContainerHelperDisplay:
    """Create one always-plain helper display for the call-time stderr."""
    stderr_stream = sys.stderr if stderr is None else stderr
    return ContainerHelperDisplay(
        stderr=stderr_stream,
        settings=settings,
        clock=clock,
    )


def _format_duration(value: int | float) -> str:
    return f"{value:.0f}s"
