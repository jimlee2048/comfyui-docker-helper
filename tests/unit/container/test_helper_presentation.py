"""Semantic contracts for always-plain Container helper presentation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from io import StringIO

import pytest

from comfyui_docker_helper.cli_output.policy import CliOutputSettings, OutputDetail
from comfyui_docker_helper.container.helper_events import (
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
from comfyui_docker_helper.container.presentation import (
    ContainerHelperDisplay,
    default_container_helper_display,
)


class _TerminalStream(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def isatty(self) -> bool:
        return True

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _render_detail(detail: OutputDetail) -> tuple[str, int]:
    stream = _TerminalStream()
    display = default_container_helper_display(
        CliOutputSettings(detail=detail),
        stderr=stream,
    )
    events = (
        ContainerHelperPhaseStarted(ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT),
        ContainerHelperPhaseCompleted(ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT),
        RegistryCustomNodeStarted(
            index=1,
            total=2,
            id="registry-node",
            version="1.2.3",
            pre_hook_count=1,
            post_hook_count=2,
        ),
        CustomNodeCompleted(index=1, total=2),
        GitCustomNodeStarted(
            index=2,
            total=2,
            target_name="git-node",
            pre_hook_count=0,
            post_hook_count=1,
        ),
        CustomNodeCompleted(index=2, total=2),
        ComfyUIInstallCompleted(),
        CustomNodesInstallCompleted(node_count=2),
        FinalManifestCompleted(),
    )
    for event in events:
        display.emit(event)
    return stream.getvalue(), stream.flushes


@pytest.mark.parametrize(
    ("detail", "present", "absent"),
    [
        (OutputDetail.QUIET, (), ("Checking out", "Custom node", "complete")),
        (
            OutputDetail.NORMAL,
            (
                "Checking out ComfyUI source",
                "[1/2] Custom node: registry-node 1.2.3",
                "[2/2] Custom node: git-node",
                "Custom node complete",
                "ComfyUI installation complete",
                "Custom-node installation complete",
                "Final manifest complete",
            ),
            ("Phase complete", "hooks=", "source=", "2 nodes"),
        ),
        (
            OutputDetail.VERBOSE,
            (
                "Phase complete",
                "pre-install hooks=1",
                "post-install hooks=2",
                "Custom-node installation complete: 2 nodes",
            ),
            ("source=",),
        ),
        (
            OutputDetail.DEBUG,
            ("source=registry", "source=git", "pre-install hooks=1"),
            (),
        ),
    ],
)
def test_helper_detail_semantics_are_bounded_and_plain(
    detail: OutputDetail,
    present: tuple[str, ...],
    absent: tuple[str, ...],
) -> None:
    output, flushes = _render_detail(detail)

    for value in present:
        assert value in output
    for value in absent:
        assert value not in output
    assert "\x1b" not in output
    assert "\r" not in output
    assert flushes == len(output.splitlines())


def test_fake_tty_remains_control_safe_flushed_and_append_only() -> None:
    stream = _TerminalStream()
    display = default_container_helper_display(CliOutputSettings(), stderr=stream)

    display.emit(
        RegistryCustomNodeStarted(
            index=1,
            total=1,
            id="node-name",
            version="1.0.0",
            pre_hook_count=0,
            post_hook_count=0,
        )
    )

    output = stream.getvalue()
    assert output.count("\n") == 1
    assert "node-name" in output
    assert "\x1b" not in output
    assert "\r" not in output
    assert stream.flushes == 1


def test_helper_events_reject_url_and_control_bearing_identity() -> None:
    registry_values = (
        ("https://user:secret@example.test/node", "1.0.0"),
        ("node\nname", "1.0.0"),
        ("node-name", "1.0.0\n"),
    )
    for registry_id, version in registry_values:
        with pytest.raises(ValueError):
            RegistryCustomNodeStarted(
                index=1,
                total=1,
                id=registry_id,
                version=version,
                pre_hook_count=0,
                post_hook_count=0,
            )

    for target in (
        "https://user:secret@example.test/node.git",
        "node\nname",
    ):
        with pytest.raises(ValueError, match="safe target leaf"):
            GitCustomNodeStarted(
                index=1,
                total=1,
                target_name=target,
                pre_hook_count=0,
                post_hook_count=0,
            )


def test_verbose_phase_and_command_durations_follow_event_order() -> None:
    stream = _TerminalStream()
    clock = _Clock(now=10)
    display = ContainerHelperDisplay(
        stderr=stream,
        settings=CliOutputSettings(detail=OutputDetail.VERBOSE),
        clock=clock,
    )

    display.emit(
        ContainerHelperPhaseStarted(ContainerHelperPhase.FINAL_STATE_VERIFICATION)
    )
    clock.now = 12
    display.emit(
        ContainerHelperPhaseCompleted(ContainerHelperPhase.FINAL_STATE_VERIFICATION)
    )
    clock.now = 17
    display.emit(FinalManifestCompleted())

    output = stream.getvalue()
    phase_start = output.index("Verifying final image state")
    phase_complete = output.index("Phase complete")
    command_complete = output.index("Final manifest complete")
    assert phase_start < phase_complete < command_complete
    assert "2s" in output[phase_complete:command_complete]
    assert "7s" in output[command_complete:]


@pytest.mark.parametrize(
    "events",
    [
        (ContainerHelperPhaseCompleted(ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT),),
        (
            ContainerHelperPhaseStarted(ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT),
            ContainerHelperPhaseStarted(ContainerHelperPhase.PYTORCH_INSTALLATION),
        ),
        (
            ContainerHelperPhaseStarted(ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT),
            ContainerHelperPhaseCompleted(ContainerHelperPhase.PYTORCH_INSTALLATION),
        ),
    ],
    ids=("completion-without-start", "overlapping-start", "mismatched-completion"),
)
def test_helper_display_validates_only_serial_phase_pairing(
    events: tuple[ContainerHelperEvent, ...],
) -> None:
    display = ContainerHelperDisplay(
        stderr=StringIO(),
        settings=CliOutputSettings(),
    )

    with pytest.raises(ValueError, match="phase"):
        for event in events:
            display.emit(event)


def test_helper_events_are_immutable_and_validate_safe_counts() -> None:
    event = CustomNodesInstallCompleted(node_count=2)

    with pytest.raises(FrozenInstanceError):
        event.node_count = 3  # type: ignore[misc]
    with pytest.raises(ValueError, match="non-negative"):
        CustomNodesInstallCompleted(node_count=-1)
    with pytest.raises(ValueError, match="must not exceed"):
        CustomNodeCompleted(index=2, total=1)


def test_zero_custom_nodes_keeps_truthful_phases_and_count_visible() -> None:
    stream = _TerminalStream()
    display = ContainerHelperDisplay(
        stderr=stream,
        settings=CliOutputSettings(detail=OutputDetail.NORMAL),
    )
    for phase in (
        ContainerHelperPhase.CUSTOM_NODES_PREPARATION,
        ContainerHelperPhase.CUSTOM_NODES_FINAL_VERIFICATION,
    ):
        display.emit(ContainerHelperPhaseStarted(phase))
        display.emit(ContainerHelperPhaseCompleted(phase))
    display.emit(CustomNodesInstallCompleted(node_count=0))

    output = stream.getvalue()
    assert "Preparing custom-node installation" in output
    assert "Verifying custom-node installation" in output
    assert "Custom-node installation complete: 0 nodes" in output
