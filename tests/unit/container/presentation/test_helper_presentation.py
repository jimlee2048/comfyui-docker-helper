"""Semantic contracts for always-plain Container helper presentation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from io import StringIO

import pytest

from comfyui_docker_helper.cli_output.policy import CliOutputSettings, OutputDetail
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
    LocalCustomNodeStarted,
    RegistryCustomNodeStarted,
)
from comfyui_docker_helper.container.presentation.helper import (
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
            pre_clone_hook_count=3,
            pre_hook_count=0,
            post_hook_count=1,
        ),
        ContainerHelperPhaseStarted(ContainerHelperPhase.CUSTOM_NODE_PRE_CLONE),
        ContainerHelperPhaseCompleted(ContainerHelperPhase.CUSTOM_NODE_PRE_CLONE),
        ContainerHelperPhaseStarted(
            ContainerHelperPhase.CUSTOM_NODE_SOURCE_PREPARATION
        ),
        ContainerHelperPhaseCompleted(
            ContainerHelperPhase.CUSTOM_NODE_SOURCE_PREPARATION
        ),
        CustomNodeCompleted(index=2, total=2),
        ComfyUIInstallCompleted(),
        CustomNodesInstallCompleted(node_count=2),
        FinalManifestCompleted(),
    )
    for event in events:
        display.emit(event)
    return stream.getvalue(), stream.flushes


@pytest.mark.parametrize("detail", list(OutputDetail))
def test_helper_detail_preserves_event_roles_and_detail_boundaries(
    detail: OutputDetail,
) -> None:
    output, flushes = _render_detail(detail)
    lines = output.splitlines()

    if detail is OutputDetail.QUIET:
        assert lines == []
    else:
        aggregate_completion_lines = [
            line
            for line in lines
            if "custom" in line.lower()
            and "install" in line.lower()
            and "complete" in line.lower()
        ]
        assert (
            len(aggregate_completion_lines) == 1
            and not aggregate_completion_lines[0].lstrip().startswith("[")
            and (
                detail < OutputDetail.VERBOSE
                or "2 nodes" in aggregate_completion_lines[0]
            )
        )
        checks = [
            any(
                "1/2" in line and "registry-node" in line and "1.2.3" in line
                for line in lines
            ),
            any("2/2" in line and "git-node" in line for line in lines),
            sum("Custom node" in line and "complete" in line for line in lines) == 2,
            any("ComfyUI" in line and "complete" in line for line in lines),
            any("Final manifest" in line and "complete" in line for line in lines),
        ]

        if detail is OutputDetail.NORMAL:
            checks.extend(
                not any(marker in line for line in lines)
                for marker in ("Phase complete", "hooks=", "source=", "2 nodes")
            )
        elif detail >= OutputDetail.VERBOSE:
            checks.extend(
                any(marker in line for line in lines)
                for marker in (
                    "Phase complete",
                    "pre-clone hooks=3",
                    "pre-install hooks=1",
                    "post-install hooks=2",
                    "2 nodes",
                )
            )
        if detail is OutputDetail.DEBUG:
            checks.extend(
                any(marker in line for line in lines)
                for marker in ("source=registry", "source=git")
            )
        assert all(checks)

    assert "\x1b" not in output
    assert "\r" not in output
    assert flushes == len(lines)


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
                pre_clone_hook_count=0,
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
    lines = output.splitlines()
    phase_start = next(
        index for index, line in enumerate(lines) if "final image state" in line.lower()
    )
    phase_complete = next(
        index for index, line in enumerate(lines) if "phase complete" in line.lower()
    )
    command_complete = next(
        index
        for index, line in enumerate(lines)
        if "manifest" in line.lower() and "complete" in line.lower()
    )
    assert phase_start < phase_complete < command_complete
    assert "2s" in lines[phase_complete]
    assert "7s" in lines[command_complete]


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
    with pytest.raises(ValueError, match="non-negative"):
        GitCustomNodeStarted(
            index=1,
            total=1,
            target_name="node",
            pre_clone_hook_count=-1,
            pre_hook_count=0,
            post_hook_count=0,
        )


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
    lines = output.splitlines()
    assert any(
        "preparing" in line.lower() and "custom-node" in line.lower() for line in lines
    )
    assert any(
        "verifying" in line.lower() and "custom-node" in line.lower() for line in lines
    )
    assert any("0 nodes" in line for line in lines)


@pytest.mark.parametrize(
    "detail", [OutputDetail.QUIET, OutputDetail.NORMAL, OutputDetail.DEBUG]
)
def test_local_node_event_uses_safe_identity_and_existing_detail_policy(detail):
    stream = StringIO()
    display = default_container_helper_display(
        CliOutputSettings(detail=detail), stderr=stream
    )
    display.emit(
        LocalCustomNodeStarted(
            index=1,
            total=1,
            target_name="local-node",
            pre_hook_count=1,
            post_hook_count=2,
        )
    )
    output = stream.getvalue()
    if detail == OutputDetail.QUIET:
        assert output == ""
    else:
        assert "local-node" in output
        assert ("source=local" in output) == (detail == OutputDetail.DEBUG)


@pytest.mark.parametrize("target", ["../outside", "bad\nname", "", "/absolute"])
def test_local_node_event_rejects_unsafe_display_identity(target):
    with pytest.raises(ValueError):
        LocalCustomNodeStarted(
            index=1, total=1, target_name=target, pre_hook_count=0, post_hook_count=0
        )
