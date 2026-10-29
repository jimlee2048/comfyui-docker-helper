"""Shared acquired-byte clone fakes for both native destination interfaces."""

import os
from collections.abc import Callable

import pytest

from comfyui_docker_helper.filesystem.admission import AdmittedRegularFileReader


def patch_admitted_clone(
    monkeypatch: pytest.MonkeyPatch,
    clone: Callable[[AdmittedRegularFileReader, int], None],
) -> None:
    def clone_new(
        reader: AdmittedRegularFileReader, directory: int, name: str, mode: int
    ):
        descriptor = os.open(
            name, os.O_CREAT | os.O_EXCL | os.O_RDWR, mode, dir_fd=directory
        )
        try:
            clone(reader, descriptor)
        except BaseException:
            os.close(descriptor)
            os.unlink(name, dir_fd=directory)
            raise
        return descriptor

    monkeypatch.setattr(AdmittedRegularFileReader, "clone_to", clone)
    monkeypatch.setattr(AdmittedRegularFileReader, "clone_new_file", clone_new)
