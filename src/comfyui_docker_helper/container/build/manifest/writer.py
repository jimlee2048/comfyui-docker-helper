"""Exclusive creation of the final build manifest."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


class FinalManifestWriteError(Exception):
    """The final manifest file could not be created and verified."""


def write_final_manifest_file(
    path: Path,
    content: bytes,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    """Create and verify one read-only final manifest without replacement."""
    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            try:
                descriptor = os.open(
                    path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
            except OSError as error:
                if error.errno == errno.EEXIST:
                    raise FinalManifestWriteError(
                        "final manifest target already exists"
                    ) from error
                raise
            try:
                stream = os.fdopen(descriptor, "w+b")
            except BaseException:
                os.close(descriptor)
                raise
            with stream:
                written = stream.write(content)
                if written != len(content):
                    raise FinalManifestWriteError("final manifest write was incomplete")
                stream.flush()
                os.fchown(stream.fileno(), owner_uid, owner_gid)
                os.fchmod(stream.fileno(), 0o444)

                metadata = os.fstat(stream.fileno())
                stream.seek(0)
                observed = stream.read(len(content) + 1)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != owner_uid
                    or metadata.st_gid != owner_gid
                    or stat.S_IMODE(metadata.st_mode) != 0o444
                    or observed != content
                ):
                    raise FinalManifestWriteError("final manifest verification failed")

                try:
                    target_metadata = os.stat(
                        path.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise FinalManifestWriteError(
                        "final manifest target identity changed"
                    ) from error
                if not stat.S_ISREG(target_metadata.st_mode) or (
                    target_metadata.st_dev,
                    target_metadata.st_ino,
                ) != (metadata.st_dev, metadata.st_ino):
                    raise FinalManifestWriteError(
                        "final manifest target identity changed"
                    )
        finally:
            os.close(parent_fd)
    except FinalManifestWriteError:
        raise
    except OSError as error:
        raise FinalManifestWriteError(
            f"final manifest write failed: [errno {error.errno}] {error.strerror}"
        ) from error
