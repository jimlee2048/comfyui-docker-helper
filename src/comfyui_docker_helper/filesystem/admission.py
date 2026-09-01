"""Platform-native admission for cooperative local regular-file inputs."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

_close_descriptor = os.close
_platform_name = os.name
_READ_CHUNK_BYTES = 1024 * 1024
_FILE_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RESERVED_TREE_COMPONENT = ".cdh-staging"
_RESERVED_WHITEOUT_COMPONENT_PREFIX = ".wh."

type LocalTreeMemberKind = Literal["directory", "file"]
type LocalSourceKind = Literal["file", "tree"]


def local_tree_mode(kind: LocalTreeMemberKind) -> Literal["0755", "0644"]:
    """Return the fixed image mode for one selected tree kind."""
    if kind == "directory":
        return "0755"
    if kind == "file":
        return "0644"
    raise ValueError("tree member kind must be directory or file")


class TreeAdmissionError(OSError):
    """A local tree could not be admitted without exposing its host locator."""

    def __init__(
        self,
        message: str,
        *,
        relative_path: PurePosixPath | None = None,
        code: str = "invalid_source",
    ) -> None:
        self.relative_path = relative_path
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LocalTreeMember:
    """One canonical descendant record in a host-local directory tree."""

    relative_path: PurePosixPath | str
    kind: LocalTreeMemberKind
    size: int | None = None
    digest: str | None = None

    def __post_init__(self) -> None:
        path = _canonical_tree_relative_path(self.relative_path)
        object.__setattr__(self, "relative_path", path)
        if self.kind == "directory":
            if self.size is not None or self.digest is not None:
                raise ValueError("directory tree members require null content")
        elif self.kind == "file":
            if self.size is not None and self.size < 0:
                raise ValueError("regular-file tree member size must not be negative")
            if (
                self.digest is not None
                and _FILE_DIGEST_PATTERN.fullmatch(self.digest) is None
            ):
                raise ValueError(
                    "regular-file tree member digest must be sha256:<64 lowercase hex>"
                )
            if (self.size is None) != (self.digest is None):
                raise ValueError(
                    "regular-file tree member size and digest must be both set or "
                    "both null"
                )
        else:
            raise ValueError("tree member kind must be directory or file")


@dataclass(frozen=True, slots=True)
class LocalTreeInventory:
    """Complete sorted structure accepted from one local directory root."""

    members: tuple[LocalTreeMember, ...] = ()

    def __post_init__(self) -> None:
        members = tuple(self.members)
        if any(not isinstance(item, LocalTreeMember) for item in members):
            raise ValueError("local tree inventory members must be LocalTreeMember")
        object.__setattr__(self, "members", members)
        encoded_paths = tuple(
            item.relative_path.as_posix().encode("utf-8") for item in members
        )
        if encoded_paths != tuple(sorted(encoded_paths)):
            raise ValueError("local tree members must be sorted by UTF-8 path")
        if len(set(encoded_paths)) != len(encoded_paths):
            raise ValueError("local tree members must be unique")
        by_path = {item.relative_path: item for item in members}
        for item in members:
            for parent in _tree_parent_paths(item.relative_path):
                parent_item = by_path.get(parent)
                if parent_item is None or parent_item.kind != "directory":
                    raise ValueError(
                        "local tree member parents must be admitted directories"
                    )
        content_states = {
            item.size is not None and item.digest is not None
            for item in members
            if item.kind == "file"
        }
        if len(content_states) > 1:
            raise ValueError("local tree file content records must be uniformly locked")

    @property
    def empty(self) -> bool:
        return not self.members


@dataclass(frozen=True, slots=True)
class AdmittedLocalFile:
    """One safely opened regular local source with optional content identity."""

    size: int
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class AdmittedLocalSource:
    """One process-local source classified as a regular file or complete tree."""

    kind: LocalSourceKind
    file: AdmittedLocalFile | None = None
    tree: LocalTreeInventory | None = None

    def __post_init__(self) -> None:
        if self.kind == "file":
            if self.file is None or self.tree is not None:
                raise ValueError("file admission requires only a file record")
        elif self.kind == "tree":
            if self.tree is None or self.file is not None:
                raise ValueError("tree admission requires only a tree inventory")
        else:
            raise ValueError("local source kind must be file or tree")


def admit_local_source(
    path: str | os.PathLike[str],
    *,
    content_lock: bool = False,
) -> AdmittedLocalSource:
    """Admit one source as a regular file or complete real directory tree.

    A tree is enumerated by cdh rather than delegated to ``rglob`` or a copy
    helper. Every regular member is safely opened; only content-locked
    admission consumes its bytes to establish a content identity.
    """
    canonical = _canonical_absolute_source_path(path)
    if _platform_name == "nt":
        # Keep the Windows local-drive and component authority in one module;
        # Python's directory walk below only handles the admitted tree shape.
        from comfyui_docker_helper.filesystem.windows import (
            validate_local_absolute_path,
        )

        validate_local_absolute_path(canonical)
    try:
        observed = os.lstat(canonical)
    except OSError as error:
        raise TreeAdmissionError(
            "local source could not be inspected", code="source_unavailable"
        ) from error
    if _is_reparse_observation(observed):
        raise TreeAdmissionError(
            "local source must not be a link or reparse point", code="source_reparse"
        )
    if stat.S_ISREG(observed.st_mode):
        return AdmittedLocalSource(
            "file",
            file=_admit_local_file(canonical, content_lock=content_lock),
        )
    if stat.S_ISDIR(observed.st_mode):
        if _platform_name == "nt":
            from comfyui_docker_helper.filesystem.windows import (
                validate_local_directory_absolute_path,
            )

            validate_local_directory_absolute_path(canonical)
        return AdmittedLocalSource(
            "tree",
            tree=_enumerate_local_tree(canonical, content_lock=content_lock),
        )
    raise TreeAdmissionError(
        "local source must be a regular file or real directory", code="source_type"
    )


def admit_local_tree(
    path: str | os.PathLike[str],
    *,
    content_lock: bool = False,
) -> LocalTreeInventory:
    """Admit and return one complete local directory inventory."""
    admitted = admit_local_source(path, content_lock=content_lock)
    if admitted.kind != "tree" or admitted.tree is None:
        raise TreeAdmissionError(
            "local source must be a real directory", code="source_type"
        )
    return admitted.tree


def revalidate_local_tree(
    path: str | os.PathLike[str],
    expected: LocalTreeInventory,
) -> LocalTreeInventory:
    """Re-enumerate a tree and require its accepted structure to be unchanged."""
    current = admit_local_tree(path)
    changed = _first_tree_structure_difference(expected, current)
    if changed is not None:
        raise TreeAdmissionError(
            "local source directory changed during admission",
            relative_path=changed,
            code="membership_drift",
        )
    return current


def _admit_local_file(path: str, *, content_lock: bool) -> AdmittedLocalFile:
    def consume(reader: AdmittedRegularFileReader) -> tuple[int, str]:
        digest = hashlib.sha256()
        while chunk := reader.read_chunk():
            digest.update(chunk)
        return reader.size, f"sha256:{digest.hexdigest()}"

    try:
        if content_lock:
            size, digest = operate_regular_absolute_file(path, consume)
        else:
            size = observe_regular_absolute_file(path).size
            digest = None
    except (OSError, ValueError) as error:
        raise TreeAdmissionError(
            "local source file could not be read", code="source_unreadable"
        ) from error
    return AdmittedLocalFile(size, digest)


def _enumerate_local_tree(path: str, *, content_lock: bool) -> LocalTreeInventory:
    members: list[LocalTreeMember] = []
    pending: list[tuple[str, PurePosixPath]] = [(path, PurePosixPath("."))]
    while pending:
        directory, relative_directory = pending.pop()
        try:
            current = os.lstat(directory)
        except OSError as error:
            raise TreeAdmissionError(
                "local source directory could not be inspected",
                relative_path=(
                    relative_directory
                    if relative_directory != PurePosixPath(".")
                    else None
                ),
                code="traversal_failed",
            ) from error
        if _is_reparse_observation(current) or not stat.S_ISDIR(current.st_mode):
            raise TreeAdmissionError(
                "local source tree contains a non-directory traversal node",
                relative_path=(
                    relative_directory
                    if relative_directory != PurePosixPath(".")
                    else None
                ),
                code="shape_changed",
            )
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise TreeAdmissionError(
                "local source directory could not be traversed",
                relative_path=(
                    relative_directory
                    if relative_directory != PurePosixPath(".")
                    else None
                ),
                code="traversal_failed",
            ) from error
        # Validate names before sorting so an unsafe name cannot influence the
        # ordering or become part of a user-facing diagnostic accidentally.
        validated: list[tuple[bytes, os.DirEntry[str], PurePosixPath]] = []
        for entry in entries:
            name = entry.name
            relative = _join_tree_relative_path(relative_directory, name)
            validated.append((relative.as_posix().encode("utf-8"), entry, relative))
        validated.sort(key=lambda item: item[0])
        for _encoded, entry, relative in validated:
            full_path = os.fspath(entry.path)
            try:
                observed = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise TreeAdmissionError(
                    "local source member could not be inspected",
                    relative_path=relative,
                    code="member_unavailable",
                ) from error
            if _is_reparse_observation(observed):
                raise TreeAdmissionError(
                    "local source member must not be a link or reparse point",
                    relative_path=relative,
                    code="member_reparse",
                )
            if stat.S_ISDIR(observed.st_mode):
                members.append(LocalTreeMember(relative, "directory"))
                pending.append((full_path, relative))
            elif stat.S_ISREG(observed.st_mode):
                try:
                    admitted = _admit_local_file(full_path, content_lock=content_lock)
                except TreeAdmissionError as error:
                    raise TreeAdmissionError(
                        str(error), relative_path=relative, code=error.code
                    ) from error
                members.append(
                    LocalTreeMember(
                        relative,
                        "file",
                        size=admitted.size if content_lock else None,
                        digest=admitted.digest if content_lock else None,
                    )
                )
            else:
                raise TreeAdmissionError(
                    "local source member must be a real directory or regular file",
                    relative_path=relative,
                    code="member_type",
                )
    try:
        return LocalTreeInventory(tuple(sorted(members, key=_tree_member_sort_key)))
    except (UnicodeError, ValueError) as error:
        if isinstance(error, TreeAdmissionError):
            raise
        raise TreeAdmissionError(
            "local source tree inventory is invalid", code="inventory_invalid"
        ) from error


def _first_tree_structure_difference(
    expected: LocalTreeInventory,
    current: LocalTreeInventory,
) -> PurePosixPath | None:
    before = {item.relative_path: item.kind for item in expected.members}
    after = {item.relative_path: item.kind for item in current.members}
    for path in sorted(before.keys() | after.keys(), key=_tree_path_sort_key):
        if before.get(path) != after.get(path):
            return path
    return None


def _canonical_absolute_source_path(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path)
    if not isinstance(value, str):
        raise ValueError("local source path must be one canonical absolute path")
    if _platform_name == "nt":
        # The Windows parser is the authority for drive, namespace, stream,
        # DOS-device, and component rules.  The call here keeps a clear error
        # boundary while avoiding a second path implementation.
        return value
    parsed = PurePosixPath(value)
    if (
        not value
        or not parsed.is_absolute()
        or value.startswith("//")
        or "\\" in value
        or parsed.as_posix() != value
        or any(part in {"", ".", ".."} for part in parsed.parts[1:])
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("local source path must be one canonical absolute POSIX path")
    _observe_posix_directory_components(parsed)
    return value


def _observe_posix_directory_components(path: PurePosixPath) -> None:
    candidate = PurePosixPath("/")
    # The final component may be either the admitted file or directory; its
    # no-follow type is checked by ``admit_local_source``/the file reader.
    for component in path.parts[1:-1]:
        candidate /= component
        try:
            observed = os.lstat(candidate)
        except OSError as error:
            raise TreeAdmissionError(
                "local source path could not be inspected", code="source_unavailable"
            ) from error
        if _is_reparse_observation(observed) or not stat.S_ISDIR(observed.st_mode):
            raise TreeAdmissionError(
                "local source path ancestors must be real directories",
                code="source_ancestor",
            )


def _join_tree_relative_path(parent: PurePosixPath, name: str) -> PurePosixPath:
    relative = PurePosixPath(name) if parent == PurePosixPath(".") else parent / name
    try:
        _validate_tree_component(name)
    except TreeAdmissionError as error:
        if error.relative_path is None:
            raise TreeAdmissionError(
                str(error), relative_path=relative, code=error.code
            ) from error
        raise
    try:
        relative.as_posix().encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise TreeAdmissionError(
            "local source member name is not strict UTF-8",
            relative_path=relative,
            code="member_name",
        ) from error
    return relative


def _validate_tree_component(name: str) -> None:
    if _platform_name == "nt":
        from comfyui_docker_helper.filesystem.windows import (
            validate_local_tree_component,
        )

        try:
            validate_local_tree_component(name)
        except ValueError as error:
            raise TreeAdmissionError(
                "local source member name is not representable on Windows",
                code="member_name",
            ) from error
    if (
        not name
        or name in {".", ".."}
        or "\\" in name
        or "\x00" in name
        or any(unicodedata.category(character) == "Cc" for character in name)
    ):
        raise TreeAdmissionError(
            "local source member name is unsafe or reserved",
            code="member_name",
        )
    if name == _RESERVED_TREE_COMPONENT or name.startswith(
        _RESERVED_WHITEOUT_COMPONENT_PREFIX
    ):
        raise TreeAdmissionError(
            "local source member name is unsafe or reserved",
            code="reserved_member",
        )


def _canonical_tree_relative_path(value: PurePosixPath | str) -> PurePosixPath:
    raw_value = value.as_posix() if isinstance(value, PurePosixPath) else value
    path = PurePosixPath(raw_value)
    text = path.as_posix()
    if (
        path.is_absolute()
        or not path.parts
        or text != raw_value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("local tree member path must be canonical relative POSIX")
    for component in path.parts:
        try:
            _validate_tree_component(component)
        except TreeAdmissionError as error:
            raise ValueError(str(error)) from error
    try:
        text.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise ValueError("local tree member path must be strict UTF-8") from None
    return path


def _tree_parent_paths(path: PurePosixPath) -> tuple[PurePosixPath, ...]:
    return tuple(
        PurePosixPath("/").joinpath(*path.parts[:index]).relative_to("/")
        for index in range(1, len(path.parts))
    )


def _tree_path_sort_key(path: PurePosixPath) -> bytes:
    return path.as_posix().encode("utf-8", "strict")


def _tree_member_sort_key(member: LocalTreeMember) -> bytes:
    return _tree_path_sort_key(member.relative_path)


def _is_reparse_observation(observed: os.stat_result) -> bool:
    return stat.S_ISLNK(observed.st_mode) or bool(
        getattr(observed, "st_file_attributes", 0) & 0x00000400
    )


@dataclass(frozen=True, slots=True)
class AdmittedRegularFile:
    """Bytes and any reliable platform-observed mode for one admitted file."""

    data: bytes
    mode: int | None


@dataclass(frozen=True, slots=True)
class ObservedRegularFile:
    """Shape observed while streaming one admitted regular file."""

    size: int
    mode: int | None


class FileCloneUnavailableError(OSError):
    """The admitted source cannot be cloned to the requested filesystem."""


@dataclass(frozen=True, slots=True)
class AdmittedRegularFileReader:
    """Metadata and bounded reads valid only during one admitted operation."""

    size: int
    mode: int | None
    _read_chunk: Callable[[int | None], bytes] = field(repr=False)
    _clone_to: Callable[[int], None] | None = field(default=None, repr=False)

    def read_chunk(self, limit: int | None = None) -> bytes:
        """Read at most one fixed-size chunk from the admitted file."""
        return self._read_chunk(limit)

    def clone_to(self, destination_fd: int) -> None:
        """Clone the whole admitted file or report a classified unavailable case."""
        if self._clone_to is None:
            raise FileCloneUnavailableError("copy-on-write clone is unavailable")
        self._clone_to(destination_fd)


def operate_regular_absolute_file[T](
    path: str | os.PathLike[str],
    operation: Callable[[AdmittedRegularFileReader], T],
) -> T:
    """Run one operation while a regular file remains admitted and open."""
    return _operate_regular_absolute_file(path, operation)


def observe_regular_absolute_file(
    path: str | os.PathLike[str],
) -> ObservedRegularFile:
    """Observe one admitted regular file without consuming its content."""
    return operate_regular_absolute_file(
        path, lambda reader: ObservedRegularFile(reader.size, reader.mode)
    )


def consume_regular_absolute_file(
    path: str | os.PathLike[str], consume: Callable[[bytes], None]
) -> ObservedRegularFile:
    """Stream one admitted regular file in fixed chunks to ``consume``."""
    return _consume_regular_absolute_file(path, max_bytes=None, consume=consume)


def read_regular_absolute_file(path: str | os.PathLike[str]) -> bytes:
    """Read one statically checked path without following its final symlink."""
    return _read_regular_absolute_file(path, max_bytes=None).data


def read_bounded_regular_absolute_file(
    path: str | os.PathLike[str], *, max_bytes: int
) -> AdmittedRegularFile:
    """Read one admitted file and fail on its first byte beyond ``max_bytes``."""
    if max_bytes < 0:
        raise ValueError("maximum byte count must not be negative")
    return _read_regular_absolute_file(path, max_bytes=max_bytes)


def _read_regular_absolute_file(
    path: str | os.PathLike[str], *, max_bytes: int | None
) -> AdmittedRegularFile:
    value = os.fspath(path)
    if _platform_name == "nt":
        if not isinstance(value, str):
            raise ValueError("path must be one canonical absolute platform path")
        from comfyui_docker_helper.filesystem.windows import (
            read_regular_absolute_file as read_windows_regular_absolute_file,
        )

        return AdmittedRegularFile(
            read_windows_regular_absolute_file(value, max_bytes=max_bytes),
            mode=None,
        )
    chunks: list[bytes] = []
    observed = _consume_regular_absolute_file(
        path,
        max_bytes=max_bytes,
        consume=chunks.append,
    )
    return AdmittedRegularFile(b"".join(chunks), observed.mode)


def _consume_regular_absolute_file(
    path: str | os.PathLike[str],
    *,
    max_bytes: int | None,
    consume: Callable[[bytes], None],
) -> ObservedRegularFile:
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("maximum byte count must not be negative")

    def operation(reader: AdmittedRegularFileReader) -> ObservedRegularFile:
        if max_bytes is not None and reader.size > max_bytes:
            raise OSError("admitted input exceeds the maximum byte count")
        total_bytes = 0
        while True:
            read_size = None
            if max_bytes is not None:
                read_size = min(_READ_CHUNK_BYTES, max_bytes - total_bytes + 1)
            chunk = reader.read_chunk(read_size)
            if not chunk:
                return ObservedRegularFile(total_bytes, reader.mode)
            total_bytes += len(chunk)
            if max_bytes is not None and total_bytes > max_bytes:
                raise OSError("admitted input exceeds the maximum byte count")
            consume(chunk)

    return _operate_regular_absolute_file(path, operation)


def _operate_regular_absolute_file[T](
    path: str | os.PathLike[str],
    operation: Callable[[AdmittedRegularFileReader], T],
) -> T:
    value = os.fspath(path)
    if not isinstance(value, str):
        raise ValueError("path must be one canonical absolute platform path")
    if _platform_name == "nt":
        from comfyui_docker_helper.filesystem.windows import (
            operate_regular_absolute_file as operate_windows_regular_absolute_file,
        )

        return operate_windows_regular_absolute_file(
            value,
            lambda size, read_chunk: operation(
                AdmittedRegularFileReader(size, None, read_chunk)
            ),
        )

    parsed = PurePosixPath(value)
    if (
        not value
        or not parsed.is_absolute()
        or value.startswith("//")
        or "\\" in value
        or parsed.as_posix() != value
        or len(parsed.parts) < 2
        or any(part in {"", ".", ".."} for part in parsed.parts[1:])
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("path must be one canonical absolute POSIX path")
    if os.name != "posix" or any(
        not hasattr(os, name) for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")
    ):
        raise OSError("regular-file admission is unavailable")

    _observe_posix_components(parsed)
    leaf_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    leaf_fd: int | None = None
    primary_error = False
    try:
        leaf_fd = os.open(value, leaf_flags)
        before = os.fstat(leaf_fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("admitted input must be a regular file")
        if before.st_size < 0:
            raise OSError("admitted input has an invalid size")
        total_bytes = 0
        eof = False

        def read_chunk(limit: int | None = None) -> bytes:
            nonlocal eof, total_bytes
            if eof:
                return b""
            if limit is not None and limit < 1:
                raise ValueError("read limit must be positive")
            read_size = (
                _READ_CHUNK_BYTES if limit is None else min(_READ_CHUNK_BYTES, limit)
            )
            chunk = os.read(leaf_fd, read_size)
            if not chunk:
                eof = True
                return b""
            total_bytes += len(chunk)
            return chunk

        def clone_to(destination_fd: int) -> None:
            import errno
            import fcntl

            try:
                fcntl.ioctl(destination_fd, 0x40049409, leaf_fd)
            except OSError as error:
                if error.errno in {
                    errno.EINVAL,
                    errno.ENOTTY,
                    errno.EOPNOTSUPP,
                    errno.EXDEV,
                }:
                    raise FileCloneUnavailableError(
                        "copy-on-write clone is unavailable"
                    ) from error
                raise

        result = operation(
            AdmittedRegularFileReader(
                before.st_size,
                before.st_mode,
                read_chunk,
                clone_to,
            )
        )
        after = os.fstat(leaf_fd)
        if not stat.S_ISREG(after.st_mode):
            raise OSError("admitted input must be a regular file")
        if before.st_size != after.st_size or (eof and total_bytes != before.st_size):
            raise OSError("admitted input changed during its bounded read")
        return result
    except BaseException:
        primary_error = True
        raise
    finally:
        if leaf_fd is not None:
            try:
                _close_descriptor(leaf_fd)
            except OSError as error:
                if not primary_error:
                    raise error


def _observe_posix_components(path: PurePosixPath) -> None:
    """Reject links and special nodes visible during one static path walk."""
    candidate = PurePosixPath("/")
    for index, component in enumerate(path.parts[1:], start=1):
        candidate /= component
        mode = os.lstat(candidate).st_mode
        leaf = index == len(path.parts) - 1
        if stat.S_ISLNK(mode):
            raise OSError(
                "admitted input must be a regular file"
                if leaf
                else "admitted path ancestors must be real directories"
            )
        if leaf:
            if not stat.S_ISREG(mode):
                raise OSError("admitted input must be a regular file")
        elif not stat.S_ISDIR(mode):
            raise OSError("admitted path ancestors must be real directories")
