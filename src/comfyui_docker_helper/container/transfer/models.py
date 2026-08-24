"""Shared transfer adapter preparation contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from comfyui_docker_helper.container.transfer.core import DownloaderSettings


@runtime_checkable
class DownloadBackendPreparer(Protocol):
    """Optional backend hook for startup work before downloads begin."""

    def prepare(self, settings: DownloaderSettings) -> None: ...
