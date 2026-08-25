"""Shared fixtures for runtime SSH unit owners."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "test@example"
)
SECOND_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg "
    "second@example"
)


@dataclass(frozen=True, slots=True)
class CommandCall:
    argv: list[str]
    input_data: bytes
    description: str


class RecordingRunner:
    def __init__(self, returncodes: tuple[int, ...] = ()) -> None:
        self.calls: list[CommandCall] = []
        self._returncodes = list(returncodes)

    def __call__(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        input_data: bytes,
        description: str,
    ) -> int:
        self.calls.append(
            CommandCall(
                argv=list(argv),
                input_data=input_data,
                description=description,
            )
        )
        if self._returncodes:
            return self._returncodes.pop(0)
        return 0


class OwnershipRecorder:
    def __init__(self) -> None:
        self.chown_calls: list[tuple[Path, int, int]] = []
        self.chmod_calls: list[tuple[Path, int]] = []
        self.fchown_calls: list[tuple[int, int]] = []
        self.fchmod_calls: list[int] = []

    def chown(self, path: str | Path, uid: int, gid: int) -> None:
        self.chown_calls.append((Path(path), uid, gid))

    def chmod(self, path: str | Path, mode: int) -> None:
        self.chmod_calls.append((Path(path), mode))
        Path(path).chmod(mode)

    def fchown(self, descriptor: int, uid: int, gid: int) -> None:
        del descriptor
        self.fchown_calls.append((uid, gid))

    def fchmod(self, descriptor: int, mode: int) -> None:
        self.fchmod_calls.append(mode)
        os.fchmod(descriptor, mode)


def create_root_home(tmp_path: Path) -> Path:
    root_home = tmp_path / "root"
    root_home.mkdir(mode=0o700)
    return root_home
