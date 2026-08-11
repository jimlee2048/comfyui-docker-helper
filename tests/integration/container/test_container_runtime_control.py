"""Linux Unix-domain transport coverage for container runtime control."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import pytest

from comfyui_docker_helper.container import runtime_control as control_module
from comfyui_docker_helper.container.runtime_control import (
    RuntimeControlEndpointError,
    RuntimePeerCredentials,
    RuntimeRestartRequest,
    connect_runtime_control,
    open_runtime_control_listener,
    receive_runtime_control_request,
    send_runtime_control_message,
)


def _endpoint(tmp_path: Path, name: str = "control") -> Path:
    return tmp_path / name / "runtime.sock"


def test_fresh_endpoint_secures_modes_authenticates_peer_and_cleans_up(
    tmp_path: Path,
) -> None:
    endpoint = _endpoint(tmp_path)
    listener = open_runtime_control_listener(endpoint)
    client = connect_runtime_control(endpoint)
    try:
        assert stat.S_IMODE(endpoint.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(endpoint.lstat().st_mode) == 0o600
        peer = listener.accept()
        try:
            send_runtime_control_message(client, RuntimeRestartRequest())
            assert receive_runtime_control_request(peer) == RuntimeRestartRequest()
        finally:
            peer.close()
    finally:
        client.close()
        listener.close()

    assert endpoint.parent.is_dir()
    assert not endpoint.exists()


def test_missing_controller_fails_without_creating_a_fallback(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)

    with pytest.raises(RuntimeControlEndpointError, match="not available"):
        connect_runtime_control(endpoint)

    assert not endpoint.exists()


def test_live_endpoint_is_not_replaced(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    listener = open_runtime_control_listener(endpoint)
    identity = endpoint.lstat().st_ino
    try:
        with pytest.raises(RuntimeControlEndpointError, match="already owns"):
            open_runtime_control_listener(endpoint)
        assert endpoint.lstat().st_ino == identity
        assert stat.S_ISSOCK(endpoint.lstat().st_mode)
    finally:
        listener.close()


def test_same_uid_refused_stale_socket_is_replaced_once(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    endpoint.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(os.fspath(endpoint))
    stale.close()

    listener = open_runtime_control_listener(endpoint)
    client = connect_runtime_control(endpoint)
    try:
        assert stat.S_ISSOCK(endpoint.lstat().st_mode)
        peer = listener.accept()
        peer.close()
    finally:
        client.close()
        listener.close()


@pytest.mark.parametrize("disappear_call", [1, 2])
def test_stale_socket_disappearance_uses_the_single_bind_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disappear_call: int,
) -> None:
    endpoint = _endpoint(tmp_path)
    endpoint.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(os.fspath(endpoint))
    stale.close()
    original_admit = control_module._admit_stale_candidate
    calls = 0

    def disappear_during_check(
        path: Path,
        *,
        owner_uid: int,
    ) -> os.stat_result | None:
        nonlocal calls
        calls += 1
        if calls == disappear_call:
            path.unlink()
            return None
        return original_admit(path, owner_uid=owner_uid)

    monkeypatch.setattr(
        control_module,
        "_admit_stale_candidate",
        disappear_during_check,
    )

    listener = open_runtime_control_listener(endpoint)
    try:
        assert stat.S_ISSOCK(endpoint.lstat().st_mode)
    finally:
        listener.close()


def test_wrong_owner_stale_candidate_is_not_removed(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    endpoint.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(os.fspath(endpoint))
    stale.close()

    with pytest.raises(RuntimeControlEndpointError, match="unsafe"):
        control_module._admit_stale_candidate(
            endpoint,
            owner_uid=os.geteuid() + 1,
        )

    assert stat.S_ISSOCK(endpoint.lstat().st_mode)


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_unsafe_existing_endpoint_fails_closed(tmp_path: Path, kind: str) -> None:
    endpoint = _endpoint(tmp_path, kind)
    endpoint.parent.mkdir(mode=0o700)
    if kind == "file":
        endpoint.write_text("not a socket", encoding="utf-8")
    elif kind == "directory":
        endpoint.mkdir()
    else:
        target = endpoint.parent / "target"
        target.write_text("not a socket", encoding="utf-8")
        endpoint.symlink_to(target)

    with pytest.raises(RuntimeControlEndpointError, match="unsafe"):
        open_runtime_control_listener(endpoint)

    assert endpoint.exists() or endpoint.is_symlink()


def test_wrong_uid_peer_is_closed_before_request_processing(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)

    def wrong_uid(_peer: socket.socket) -> RuntimePeerCredentials:
        return RuntimePeerCredentials(pid=1, uid=os.geteuid() + 1, gid=os.getegid())

    listener = open_runtime_control_listener(
        endpoint,
        peer_credential_reader=wrong_uid,
    )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(os.fspath(endpoint))
        with pytest.raises(RuntimeControlEndpointError, match="does not match"):
            listener.accept()
    finally:
        client.close()
        listener.close()


def test_stale_identity_change_is_not_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _endpoint(tmp_path)
    endpoint.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(os.fspath(endpoint))
    stale.close()
    original_admit = control_module._admit_stale_candidate
    calls = 0

    def replace_before_confirmation(
        path: Path,
        *,
        owner_uid: int,
    ) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            path.unlink()
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            replacement.bind(os.fspath(path))
            replacement.close()
        return original_admit(path, owner_uid=owner_uid)

    monkeypatch.setattr(
        control_module,
        "_admit_stale_candidate",
        replace_before_confirmation,
    )

    with pytest.raises(RuntimeControlEndpointError, match="changed"):
        open_runtime_control_listener(endpoint)

    assert stat.S_ISSOCK(endpoint.lstat().st_mode)


def test_listener_close_preserves_path_replacement(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    listener = open_runtime_control_listener(endpoint)
    endpoint.unlink()
    endpoint.write_text("replacement", encoding="utf-8")

    listener.close()

    assert endpoint.read_text(encoding="utf-8") == "replacement"


def test_listener_records_identity_after_securing_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _endpoint(tmp_path)
    original_chmod = Path.chmod

    def chmod_and_change_timestamp(path: Path, mode: int) -> None:
        original_chmod(path, mode)
        if path == endpoint:
            secured = path.lstat()
            os.utime(
                path,
                ns=(secured.st_atime_ns, secured.st_mtime_ns + 1_000_000_000),
                follow_symlinks=False,
            )

    monkeypatch.setattr(Path, "chmod", chmod_and_change_timestamp)

    listener = open_runtime_control_listener(endpoint)
    try:
        assert listener.endpoint_identity == control_module._endpoint_identity(
            endpoint.lstat()
        )
    finally:
        listener.close()

    assert not endpoint.exists()


def test_listener_close_does_not_raise_when_cleanup_inspection_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _endpoint(tmp_path)
    listener = open_runtime_control_listener(endpoint)
    original_lstat = Path.lstat

    def fail_endpoint_lstat(path: Path) -> os.stat_result:
        if path == endpoint:
            raise PermissionError("synthetic cleanup failure")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_endpoint_lstat)

    listener.close()

    assert listener.socket.fileno() == -1
