"""Narrow shared CLI output types."""

from comfyui_docker_helper.cli_output.events import EventSink
from comfyui_docker_helper.cli_output.policy import CliOutputSettings, OutputDetail

__all__ = [
    "CliOutputSettings",
    "EventSink",
    "OutputDetail",
]
