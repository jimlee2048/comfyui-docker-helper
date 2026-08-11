"""Native Windows evidence for creation-time private host state."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from comfyui_docker_helper import _windows_files
from comfyui_docker_helper.host import private_state

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows security descriptors and CRT descriptors",
)


def test_windows_private_state_has_exact_protected_trustees_from_creation() -> None:
    import win32api
    import win32security

    root = private_state.create_private_directory(prefix="cdh-private-test-")
    secret = root / "secret"
    lock = root / "lock"
    secret_fd = -1
    first_lock_fd = -1
    second_lock_fd = -1
    try:
        secret_fd = private_state.create_private_file(secret)
        os.write(secret_fd, b"secret")
        os.close(secret_fd)
        secret_fd = -1
        first_lock_fd = private_state.open_private_lock_file(lock)
        second_lock_fd = private_state.open_private_lock_file(lock)
        os.write(first_lock_fd, b"x")
        os.lseek(second_lock_fd, 0, os.SEEK_SET)
        assert os.read(second_lock_fd, 1) == b"x"

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        try:
            user_sid, _attributes = win32security.GetTokenInformation(
                token, win32security.TokenUser
            )
        finally:
            token.Close()
        system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)
        trustee_strings = {
            win32security.ConvertSidToStringSid(user_sid),
            win32security.ConvertSidToStringSid(system_sid),
        }
        _assert_private_security(
            root,
            directory=True,
            owner_sid=user_sid,
            trustee_strings=trustee_strings,
            win32security=win32security,
        )
        for path in (secret, lock):
            _assert_private_security(
                path,
                directory=False,
                owner_sid=user_sid,
                trustee_strings=trustee_strings,
                win32security=win32security,
            )
    finally:
        for descriptor in (second_lock_fd, first_lock_fd, secret_fd):
            if descriptor >= 0:
                os.close(descriptor)
        shutil.rmtree(root, ignore_errors=True)


def _assert_private_security(
    path: Path,
    *,
    directory: bool,
    owner_sid: object,
    trustee_strings: set[str],
    win32security: object,
) -> None:
    descriptor = win32security.GetNamedSecurityInfo(
        os.fspath(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION,
    )
    assert win32security.ConvertSidToStringSid(
        descriptor.GetSecurityDescriptorOwner()
    ) == win32security.ConvertSidToStringSid(owner_sid)
    control, _revision = descriptor.GetSecurityDescriptorControl()
    assert control & win32security.SE_DACL_PROTECTED
    dacl = descriptor.GetSecurityDescriptorDacl()
    inheritance = (
        _windows_files._OBJECT_INHERIT_ACE | _windows_files._CONTAINER_INHERIT_ACE
        if directory
        else 0
    )
    entries = {
        (
            win32security.ConvertSidToStringSid(dacl.GetAce(index)[2]),
            dacl.GetAce(index)[1],
            dacl.GetAce(index)[0],
        )
        for index in range(dacl.GetAceCount())
    }
    assert entries == {
        (
            trustee,
            _windows_files._FILE_ALL_ACCESS,
            (_windows_files._ACCESS_ALLOWED_ACE_TYPE, inheritance),
        )
        for trustee in trustee_strings
    }
