"""Narrow Win32 regular-file admission primitives."""

from __future__ import annotations

import ntpath
import os
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol, cast

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_READ_CONTROL = 0x00020000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_FILE_TYPE_DISK = 0x00000001
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4
_DRIVE_CDROM = 5
_DRIVE_RAMDISK = 6
_FILE_ALL_ACCESS = 0x001F01FF
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_ACCESS_ALLOWED_ACE_TYPE = 0x00
_ACL_REVISION_DS = 4
_SE_DACL_PROTECTED = 0x1000
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_FILE_OBJECT = 1
_TOKEN_QUERY = 0x0008
_O_BINARY = getattr(os, "O_BINARY", 0)
_READ_CHUNK_BYTES = 1024 * 1024
_PRIVATE_DIRECTORY_ATTEMPTS = 128

_LOCAL_DRIVE_TYPES = {
    _DRIVE_REMOVABLE,
    _DRIVE_FIXED,
    _DRIVE_CDROM,
    _DRIVE_RAMDISK,
}
_INVALID_COMPONENT_CHARACTERS = frozenset('<>"|?*:')
_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{number}" for number in "123456789¹²³"),
    *(f"LPT{number}" for number in "123456789¹²³"),
}
_INVALID_PATH_MESSAGE = "path must be one canonical absolute local Windows path"


class _WindowsApi(Protocol):
    def create_file(
        self,
        path: str,
        desired_access: int,
        share_mode: int,
        creation_disposition: int,
        flags_and_attributes: int,
    ) -> object: ...

    def get_drive_type(self, root: str) -> int: ...

    def get_file_attributes(self, path: str) -> int: ...

    def get_file_type(self, handle: object) -> int: ...

    def get_file_information(self, handle: object) -> tuple[object, ...]: ...

    def read_file(self, handle: object, size: int) -> bytes: ...

    def close_handle(self, handle: object) -> None: ...


class _PrivateWindowsApi(_WindowsApi, Protocol):
    def private_security_attributes(self, *, directory: bool) -> object: ...

    def create_directory(self, path: str, security_attributes: object) -> bool: ...

    def create_private_file(
        self,
        path: str,
        desired_access: int,
        share_mode: int,
        creation_disposition: int,
        flags_and_attributes: int,
        security_attributes: object,
    ) -> object | None: ...

    def verify_private_security(self, handle: object, *, directory: bool) -> None: ...

    def remove_directory(self, path: str) -> None: ...

    def delete_file(self, path: str) -> None: ...

    def detach_handle(self, handle: object) -> int: ...

    def open_osfhandle(self, handle: int, flags: int) -> int: ...

    def close_raw_handle(self, handle: int) -> None: ...

    def set_fd_inheritable(self, descriptor: int, inheritable: bool) -> None: ...

    def close_fd(self, descriptor: int) -> None: ...


class _ClosableHandle(Protocol):
    def Close(self) -> None: ...

    def Detach(self) -> int: ...


class _PyWin32Api:
    """Load pywin32 only when the Windows backend is actually selected."""

    def __init__(self) -> None:
        import pywintypes
        import win32file

        self._error_type = pywintypes.error
        self._win32file = win32file

    def create_file(
        self,
        path: str,
        desired_access: int,
        share_mode: int,
        creation_disposition: int,
        flags_and_attributes: int,
    ) -> object:
        return self._call(
            "CreateFile",
            lambda: self._win32file.CreateFile(
                path,
                desired_access,
                share_mode,
                None,
                creation_disposition,
                flags_and_attributes,
                None,
            ),
        )

    def get_drive_type(self, root: str) -> int:
        return int(
            self._call("GetDriveType", lambda: self._win32file.GetDriveType(root))
        )

    def get_file_attributes(self, path: str) -> int:
        attributes = int(
            self._call(
                "GetFileAttributes", lambda: self._win32file.GetFileAttributes(path)
            )
        )
        if attributes == _INVALID_FILE_ATTRIBUTES:
            raise OSError("Win32 GetFileAttributes failed")
        return attributes

    def get_file_type(self, handle: object) -> int:
        return int(
            self._call("GetFileType", lambda: self._win32file.GetFileType(handle))
        )

    def get_file_information(self, handle: object) -> tuple[object, ...]:
        return tuple(
            self._call(
                "GetFileInformationByHandle",
                lambda: self._win32file.GetFileInformationByHandle(handle),
            )
        )

    def read_file(self, handle: object, size: int) -> bytes:
        error, data = self._call(
            "ReadFile", lambda: self._win32file.ReadFile(handle, size)
        )
        if error:
            raise OSError("Win32 regular-file read failed")
        return bytes(data)

    def close_handle(self, handle: object) -> None:
        self._call("CloseHandle", lambda: cast(_ClosableHandle, handle).Close())

    def _call[T](self, operation: str, function: Callable[[], T]) -> T:
        try:
            return function()
        except self._error_type as error:
            code = getattr(error, "winerror", None)
            if isinstance(code, int):
                raise OSError(code, f"Win32 {operation} failed") from None
            raise OSError(f"Win32 {operation} failed") from None


class _PyWin32PrivateApi(_PyWin32Api):
    """Public pywin32 primitives needed only for private host state."""

    def __init__(self) -> None:
        super().__init__()
        import msvcrt

        import win32api
        import win32security

        self._msvcrt = msvcrt
        self._win32api = win32api
        self._win32security = win32security
        self._private_principals: tuple[object, object, tuple[str, ...]] | None = None

    def private_security_attributes(self, *, directory: bool) -> object:
        return self._call(
            "BuildPrivateSecurityAttributes",
            lambda: self._build_private_security_attributes(directory=directory),
        )

    def create_directory(self, path: str, security_attributes: object) -> bool:
        try:
            self._win32file.CreateDirectory(path, security_attributes)
        except self._error_type as error:
            if self._win32_error_code(error) in {80, 183}:
                return False
            raise OSError("Win32 CreateDirectory failed") from None
        return True

    def create_private_file(
        self,
        path: str,
        desired_access: int,
        share_mode: int,
        creation_disposition: int,
        flags_and_attributes: int,
        security_attributes: object,
    ) -> object | None:
        try:
            return self._win32file.CreateFile(
                path,
                desired_access,
                share_mode,
                security_attributes,
                creation_disposition,
                flags_and_attributes,
                None,
            )
        except self._error_type as error:
            if creation_disposition == _CREATE_NEW and self._win32_error_code(
                error
            ) in {80, 183}:
                return None
            raise OSError("Win32 CreateFile failed") from None

    def verify_private_security(self, handle: object, *, directory: bool) -> None:
        self._call(
            "GetSecurityInfo",
            lambda: self._verify_private_security(handle, directory=directory),
        )

    def remove_directory(self, path: str) -> None:
        self._call("RemoveDirectory", lambda: self._win32file.RemoveDirectory(path))

    def delete_file(self, path: str) -> None:
        self._call("DeleteFile", lambda: self._win32file.DeleteFile(path))

    def detach_handle(self, handle: object) -> int:
        return int(
            self._call("DetachHandle", lambda: cast(_ClosableHandle, handle).Detach())
        )

    def open_osfhandle(self, handle: int, flags: int) -> int:
        return int(self._msvcrt.open_osfhandle(handle, flags))

    def close_raw_handle(self, handle: int) -> None:
        self._call("CloseHandle", lambda: self._win32api.CloseHandle(handle))

    def set_fd_inheritable(self, descriptor: int, inheritable: bool) -> None:
        os.set_inheritable(descriptor, inheritable)

    def close_fd(self, descriptor: int) -> None:
        os.close(descriptor)

    def _build_private_security_attributes(self, *, directory: bool) -> object:
        user_sid, system_sid, trustee_strings = self._get_private_principals()
        dacl = self._win32security.ACL(128, _ACL_REVISION_DS)
        inheritance = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE if directory else 0
        for sid, _sid_string in zip(
            (user_sid, system_sid), trustee_strings, strict=False
        ):
            dacl.AddAccessAllowedAceEx(
                _ACL_REVISION_DS,
                inheritance,
                _FILE_ALL_ACCESS,
                sid,
            )
        security_attributes = self._win32security.SECURITY_ATTRIBUTES()
        security_attributes.bInheritHandle = False
        descriptor = security_attributes.SECURITY_DESCRIPTOR
        descriptor.SetSecurityDescriptorOwner(user_sid, False)
        descriptor.SetSecurityDescriptorDacl(True, dacl, False)
        descriptor.SetSecurityDescriptorControl(_SE_DACL_PROTECTED, _SE_DACL_PROTECTED)
        return security_attributes

    def _verify_private_security(self, handle: object, *, directory: bool) -> None:
        _user_sid, _system_sid, trustee_strings = self._get_private_principals()
        descriptor = self._win32security.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        )
        owner = descriptor.GetSecurityDescriptorOwner()
        owner_string = self._win32security.ConvertSidToStringSid(owner)
        if owner_string != trustee_strings[0]:
            raise OSError("private state owner cannot be verified")
        control, _revision = descriptor.GetSecurityDescriptorControl()
        if not int(control) & _SE_DACL_PROTECTED:
            raise OSError("private state DACL is not protected")
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            raise OSError("private state DACL cannot be verified")
        inheritance = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE if directory else 0
        expected = {
            (sid_string, _FILE_ALL_ACCESS, inheritance)
            for sid_string in trustee_strings
        }
        actual: list[tuple[str, int, int]] = []
        for index in range(int(dacl.GetAceCount())):
            ace = dacl.GetAce(index)
            if not isinstance(ace, tuple) or len(ace) != 3:
                raise OSError("private state DACL cannot be verified")
            header, mask, sid = ace
            if (
                not isinstance(header, tuple)
                or len(header) != 2
                or int(header[0]) != _ACCESS_ALLOWED_ACE_TYPE
            ):
                raise OSError("private state DACL cannot be verified")
            actual.append(
                (
                    self._win32security.ConvertSidToStringSid(sid),
                    int(mask),
                    int(header[1]),
                )
            )
        if len(actual) != len(expected) or set(actual) != expected:
            raise OSError("private state DACL trustees cannot be verified")

    def _get_private_principals(self) -> tuple[object, object, tuple[str, ...]]:
        if self._private_principals is not None:
            return self._private_principals
        token = self._call(
            "OpenProcessToken",
            lambda: self._win32security.OpenProcessToken(
                self._win32api.GetCurrentProcess(), _TOKEN_QUERY
            ),
        )
        try:
            user_sid, _attributes = self._call(
                "GetTokenInformation",
                lambda: self._win32security.GetTokenInformation(
                    token, self._win32security.TokenUser
                ),
            )
        finally:
            self._call("CloseToken", lambda: cast(_ClosableHandle, token).Close())
        system_sid = self._call(
            "CreateWellKnownSid",
            lambda: self._win32security.CreateWellKnownSid(
                self._win32security.WinLocalSystemSid
            ),
        )
        user_string = self._win32security.ConvertSidToStringSid(user_sid)
        system_string = self._win32security.ConvertSidToStringSid(system_sid)
        if user_string == system_string:
            system_sid = user_sid
            trustee_strings = (user_string,)
        else:
            trustee_strings = (user_string, system_string)
        self._private_principals = user_sid, system_sid, trustee_strings
        return self._private_principals

    @staticmethod
    def _win32_error_code(error: BaseException) -> int | None:
        code = getattr(error, "winerror", None)
        if isinstance(code, int):
            return code
        if error.args and isinstance(error.args[0], int):
            return error.args[0]
        return None


@dataclass(frozen=True, slots=True)
class _ParsedWindowsPath:
    drive_root: str
    components: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _HandleObservation:
    attributes: int
    size: int


def read_regular_absolute_file(path: str, *, max_bytes: int | None) -> bytes:
    """Read one regular local Windows file through its admitted Win32 handle."""
    return _read_regular_absolute_file(path, max_bytes=max_bytes, api=_PyWin32Api())


def validate_local_absolute_path(path: str) -> None:
    """Reject unsupported Windows namespaces before filesystem traversal."""
    parsed = _parse_windows_regular_file_path(path, allow_drive_root=True)
    if _PyWin32Api().get_drive_type(parsed.drive_root) not in _LOCAL_DRIVE_TYPES:
        raise OSError("path requires a verifiable local drive")


def create_private_directory(parent: str, *, prefix: str) -> str:
    """Create and verify one private random directory below a canonical parent."""
    return _create_private_directory_windows(
        parent,
        prefix=prefix,
        api=_PyWin32PrivateApi(),
        candidate_suffix=lambda: secrets.token_hex(16),
    )


def create_private_file(path: str) -> int:
    """Exclusively create a private regular file and transfer its descriptor."""
    return _open_private_file_windows(
        path,
        exclusive=True,
        read_write=False,
        api=_PyWin32PrivateApi(),
    )


def open_private_lock_file(path: str) -> int:
    """Create or open one verified private read-write lock file."""
    return _open_private_file_windows(
        path,
        exclusive=False,
        read_write=True,
        api=_PyWin32PrivateApi(),
    )


def _read_regular_absolute_file(
    path: str,
    *,
    max_bytes: int | None,
    api: _WindowsApi,
) -> bytes:
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("maximum byte count must not be negative")
    parsed = _parse_windows_regular_file_path(path)
    if api.get_drive_type(parsed.drive_root) not in _LOCAL_DRIVE_TYPES:
        raise OSError("regular-file admission requires a verifiable local drive")
    _observe_windows_components(parsed, api=api)

    leaf_handle: object | None = None
    primary_error = False
    try:
        leaf_handle = api.create_file(
            path,
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS
            | _FILE_FLAG_OPEN_REPARSE_POINT
            | _FILE_FLAG_SEQUENTIAL_SCAN,
        )
        before = _observe_handle(api, leaf_handle)
        _require_regular_file(api, leaf_handle, before)
        if max_bytes is not None and before.size > max_bytes:
            raise OSError("admitted input exceeds the maximum byte count")

        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            read_size = _READ_CHUNK_BYTES
            if max_bytes is not None:
                read_size = min(read_size, max_bytes - total_bytes + 1)
            chunk = api.read_file(leaf_handle, read_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if max_bytes is not None and total_bytes > max_bytes:
                raise OSError("admitted input exceeds the maximum byte count")
            chunks.append(chunk)

        after = _observe_handle(api, leaf_handle)
        _require_regular_file(api, leaf_handle, after)
        if before.size != after.size or total_bytes != before.size:
            raise OSError("admitted input changed during its bounded read")
        return b"".join(chunks)
    except BaseException:
        primary_error = True
        raise
    finally:
        if leaf_handle is not None:
            try:
                api.close_handle(leaf_handle)
            except OSError as error:
                if not primary_error:
                    raise error


def _observe_windows_components(path: _ParsedWindowsPath, *, api: _WindowsApi) -> None:
    """Reject reparse points and special nodes visible in one static path walk."""
    candidate = path.drive_root.rstrip("\\")
    for index, component in enumerate(path.components):
        candidate = f"{candidate}\\{component}"
        attributes = api.get_file_attributes(candidate)
        leaf = index == len(path.components) - 1
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(
                "admitted input must be a regular local file"
                if leaf
                else "admitted path ancestors must be real local directories"
            )
        directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
        if leaf:
            if directory:
                raise OSError("admitted input must be a regular local file")
        elif not directory:
            raise OSError("admitted path ancestors must be real local directories")


def _create_private_directory_windows(
    parent: str,
    *,
    prefix: str,
    api: _PrivateWindowsApi,
    candidate_suffix: Callable[[], str],
) -> str:
    _require_private_prefix(prefix)
    parsed = _parse_windows_regular_file_path(parent, allow_drive_root=True)
    if api.get_drive_type(parsed.drive_root) not in _LOCAL_DRIVE_TYPES:
        raise OSError("private state requires a verifiable local drive")
    handles: list[object] = []
    created_path: str | None = None
    try:
        security_attributes = api.private_security_attributes(directory=True)
        for _attempt in range(_PRIVATE_DIRECTORY_ATTEMPTS):
            candidate = f"{prefix}{candidate_suffix()}"
            if not _valid_component(candidate):
                raise ValueError(
                    "private directory candidate is not one safe component"
                )
            candidate_path = ntpath.join(parent, candidate)
            if not api.create_directory(candidate_path, security_attributes):
                continue
            created_path = candidate_path
            directory_handle = api.create_file(
                candidate_path,
                _READ_CONTROL,
                _FILE_SHARE_READ,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            )
            handles.append(directory_handle)
            directory = _observe_handle(api, directory_handle)
            _require_directory(api, directory_handle, directory)
            api.verify_private_security(directory_handle, directory=True)
            _close_windows_handles(api, handles)
            created_path = None
            return ntpath.join(parent, candidate)
        raise FileExistsError("private directory candidate space is unavailable")
    except BaseException:
        _suppress_close_windows_handles(api, handles)
        if created_path is not None:
            with suppress(OSError):
                api.remove_directory(created_path)
        raise


def _open_private_file_windows(
    path: str,
    *,
    exclusive: bool,
    read_write: bool,
    api: _PrivateWindowsApi,
) -> int:
    if exclusive == read_write:
        raise ValueError("private file mode is invalid")
    parsed = _parse_windows_regular_file_path(path)
    if api.get_drive_type(parsed.drive_root) not in _LOCAL_DRIVE_TYPES:
        raise OSError("private state requires a verifiable local drive")
    handles: list[object] = []
    created = False
    descriptor: int | None = None
    try:
        security_attributes = api.private_security_attributes(directory=False)
        desired_access = _GENERIC_WRITE | _READ_CONTROL
        share_mode = _FILE_SHARE_READ
        descriptor_flags = os.O_WRONLY | _O_BINARY
        if read_write:
            desired_access |= _GENERIC_READ
            share_mode |= _FILE_SHARE_WRITE
            descriptor_flags = os.O_RDWR | _O_BINARY
        leaf_handle = api.create_private_file(
            path,
            desired_access,
            share_mode,
            _CREATE_NEW,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            security_attributes,
        )
        if leaf_handle is None:
            if exclusive:
                raise FileExistsError("private file already exists")
            leaf_handle = api.create_private_file(
                path,
                desired_access,
                share_mode,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                security_attributes,
            )
            if leaf_handle is None:  # pragma: no cover - adapter contract
                raise OSError("private lock file cannot be opened")
        else:
            created = True
        handles.append(leaf_handle)
        leaf = _observe_handle(api, leaf_handle)
        _require_regular_file(api, leaf_handle, leaf)
        api.verify_private_security(leaf_handle, directory=False)

        raw_handle = api.detach_handle(leaf_handle)
        handles.pop()
        try:
            descriptor = api.open_osfhandle(raw_handle, descriptor_flags)
        except BaseException:
            with suppress(OSError):
                api.close_raw_handle(raw_handle)
            raise
        api.set_fd_inheritable(descriptor, False)
        _close_windows_handles(api, handles)
        result = descriptor
        descriptor = None
        return result
    except BaseException:
        if descriptor is not None:
            with suppress(OSError):
                api.close_fd(descriptor)
        _suppress_close_windows_handles(api, handles)
        if created:
            with suppress(OSError):
                api.delete_file(path)
        raise


def _require_private_prefix(prefix: str) -> None:
    if (
        not prefix
        or len(prefix) > 64
        or not prefix.isascii()
        or any(not (character.isalnum() or character in "-_") for character in prefix)
    ):
        raise ValueError("private directory prefix must be one short ASCII component")


def _close_windows_handles(api: _WindowsApi, handles: list[object]) -> None:
    close_error: OSError | None = None
    while handles:
        try:
            api.close_handle(handles.pop())
        except OSError as error:
            if close_error is None:
                close_error = error
    if close_error is not None:
        raise close_error


def _suppress_close_windows_handles(api: _WindowsApi, handles: list[object]) -> None:
    with suppress(OSError):
        _close_windows_handles(api, handles)


def _parse_windows_regular_file_path(
    path: str, *, allow_drive_root: bool = False
) -> _ParsedWindowsPath:
    if not path or "/" in path or ntpath.normpath(path) != path:
        raise ValueError(_INVALID_PATH_MESSAGE)
    drive, tail = ntpath.splitdrive(path)
    if (
        len(drive) != 2
        or drive[1] != ":"
        or drive[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        or not tail.startswith("\\")
    ):
        raise ValueError(_INVALID_PATH_MESSAGE)
    components = tuple(tail.split("\\")[1:])
    if allow_drive_root and components == ("",):
        components = ()
    if (not components and not allow_drive_root) or any(
        not _valid_component(part) for part in components
    ):
        raise ValueError(_INVALID_PATH_MESSAGE)
    return _ParsedWindowsPath(f"{drive[0].upper()}:\\", components)


def _valid_component(component: str) -> bool:
    if (
        not component
        or component in {".", ".."}
        or component.endswith((" ", "."))
        or any(character in _INVALID_COMPONENT_CHARACTERS for character in component)
        or any(ord(character) < 32 or ord(character) == 127 for character in component)
    ):
        return False
    basename = component.split(".", maxsplit=1)[0].rstrip(" ").upper()
    return basename not in _RESERVED_BASENAMES


def _observe_handle(api: _WindowsApi, handle: object) -> _HandleObservation:
    information = api.get_file_information(handle)
    if len(information) != 10:
        raise OSError("Win32 file metadata is unavailable")
    try:
        attributes = int(information[0])
        size = (int(information[5]) << 32) | int(information[6])
    except (TypeError, ValueError):
        raise OSError("Win32 file metadata is unavailable") from None
    if size < 0:
        raise OSError("Win32 file metadata is unavailable")
    return _HandleObservation(attributes, size)


def _require_directory(
    api: _WindowsApi, handle: object, observation: _HandleObservation
) -> None:
    if (
        api.get_file_type(handle) != _FILE_TYPE_DISK
        or not observation.attributes & _FILE_ATTRIBUTE_DIRECTORY
        or observation.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise OSError("admitted path ancestors must be real local directories")


def _require_regular_file(
    api: _WindowsApi, handle: object, observation: _HandleObservation
) -> None:
    if (
        api.get_file_type(handle) != _FILE_TYPE_DISK
        or observation.attributes & _FILE_ATTRIBUTE_DIRECTORY
        or observation.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise OSError("admitted input must be a regular local file")
