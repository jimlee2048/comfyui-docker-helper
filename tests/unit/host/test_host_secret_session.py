"""Command-scoped host Secret session and helper-adapter contracts."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfyui_docker_helper.config.diagnostics import DiagnosticSeverity
from comfyui_docker_helper.config.service import load_validate_config_result
from comfyui_docker_helper.host import git_credential_helper as helper_module
from comfyui_docker_helper.host import git_credential_process as process_module
from comfyui_docker_helper.host import secret_session as secret_session_module
from comfyui_docker_helper.host.secret_session import (
    GIT_CREDENTIAL_SESSION_ENV,
    HostSecretSession,
    HostSecretSessionError,
)

_MINIMAL_CONFIG = """
[compute_platform]
type = "cuda"
[compute_platform.cuda]
version = "13.0.3"
[pytorch]
version = "2.12.1"
[comfyui]
version = "0.11.0"
install_manager = false
"""

_POSIX_SECRET_SOURCE = pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX environment bytes or mode evidence"
)


def _configuration(
    tmp_path: Path,
    *,
    source: str,
    second_route: bool = False,
):
    config_dir = tmp_path / "configuration"
    config_dir.mkdir(exist_ok=True)
    config = config_dir / "config.toml"
    routes = """
[[cdh.git.credentials]]
match = "https://example.test/"
username = "root-user"
password = { secret = "root_token" }
"""
    if second_route:
        routes += """
[[cdh.git.credentials]]
match = "https://example.test/team/"
username = "team-user"
password = { secret = "team_token" }
"""
    config.write_text(
        _MINIMAL_CONFIG
        + f"""
[secrets.root_token]
{source}
[secrets.team_token]
env = "CDH_TEST_TEAM_TOKEN"
{routes}
"""
    )
    return load_validate_config_result(config)


def _helper(
    session: HostSecretSession,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    payload: bytes,
) -> tuple[int, bytes]:
    binding = session.git_binding()
    assert binding is not None
    output = io.BytesIO()
    with monkeypatch.context() as helper_patch:
        helper_patch.setenv(GIT_CREDENTIAL_SESSION_ENV, os.fspath(session.root))
        helper_patch.setattr(helper_module.sys, "argv", ["helper", operation])
        helper_patch.setattr(
            helper_module.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload))
        )
        helper_patch.setattr(
            helper_module.sys, "stdout", SimpleNamespace(buffer=output)
        )
        return helper_module.main(), output.getvalue()


@_POSIX_SECRET_SOURCE
def test_session_binding_and_snapshot_are_private_exact_and_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = b"\xff=synthetic password"
    reads = 0

    class CountingEnvironment(dict[bytes, bytes]):
        def get(self, key: bytes, default=None):
            nonlocal reads
            reads += 1
            return super().get(key, default)

    monkeypatch.setattr(
        secret_session_module.os,
        "environb",
        CountingEnvironment({b"CDH_TEST_ROOT_TOKEN": value}),
    )
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')

    with HostSecretSession.from_configuration(result) as session:
        binding = session.git_binding()
        assert binding is not None
        assert binding.config_args == (
            "-c",
            "credential.helper=",
            "-c",
            binding.config_args[3],
            "-c",
            "credential.useHttpPath=true",
            "-c",
            "credential.interactive=false",
        )
        assert binding.config_args[3].startswith("credential.helper=!")
        assert (
            " -m comfyui_docker_helper.host.git_credential_helper"
            in (binding.config_args[3])
        )
        assert binding.environment == {
            GIT_CREDENTIAL_SESSION_ENV: os.fspath(session.root)
        }
        assert stat.S_IMODE(session.root.stat().st_mode) == 0o700
        assert stat.S_IMODE((session.root / "metadata.json").stat().st_mode) == 0o600
        assert value not in b"\0".join(item.encode() for item in binding.config_args)

        first = session.snapshot("root_token")
        second = session.snapshot("root_token")

        assert first == second
        assert first.read_bytes() == value
        assert stat.S_IMODE(first.stat().st_mode) == 0o600
        assert stat.S_IMODE((session.root / "lock-root_token").stat().st_mode) == 0o600
        assert reads == 1


def test_windows_git_binding_quotes_unicode_python_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')
    monkeypatch.setattr(process_module, "_platform_name", "nt")
    monkeypatch.setattr(
        secret_session_module.sys,
        "executable",
        r"D:\Program Files\Python 测试\python.exe",
    )

    with HostSecretSession.from_configuration(result) as session:
        binding = session.git_binding()

    assert binding is not None
    assert binding.config_args[3] == (
        "credential.helper=!'D:/Program Files/Python 测试/python.exe' "
        "-m comfyui_docker_helper.host.git_credential_helper"
    )


def test_windows_environment_secret_encodes_unicode_as_utf8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = "合成密钥-🔒"
    monkeypatch.setattr(secret_session_module, "_platform_name", "nt")
    monkeypatch.setenv("CDH_TEST_ROOT_TOKEN", value)
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')

    with HostSecretSession.from_configuration(result) as session:
        assert session.snapshot("root_token").read_bytes() == value.encode("utf-8")


@_POSIX_SECRET_SOURCE
def test_empty_value_fails_at_first_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')
    monkeypatch.setattr(
        secret_session_module.os,
        "environb",
        {b"CDH_TEST_ROOT_TOKEN": b""},
    )

    with (
        HostSecretSession.from_configuration(result) as session,
        pytest.raises(HostSecretSessionError) as raised,
    ):
        session.snapshot("root_token")

    assert raised.value.code == "invalid_value"


@_POSIX_SECRET_SOURCE
@pytest.mark.parametrize(
    "value",
    [
        b"synthetic-sensitive-marker\n",
        b"synthetic-sensitive-marker\r",
        b"synthetic-sensitive-marker\0",
        b"synthetic-sensitive-marker" + b"x" * (65_526 - 26),
    ],
    ids=("lf", "cr", "nul", "oversized"),
)
def test_invalid_value_failures_do_not_echo_input(
    value: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "synthetic-sensitive-marker"
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')
    monkeypatch.setattr(
        secret_session_module.os,
        "environb",
        {b"CDH_TEST_ROOT_TOKEN": value},
    )

    with (
        HostSecretSession.from_configuration(result) as session,
        pytest.raises(HostSecretSessionError) as raised,
    ):
        session.snapshot("root_token")

    assert raised.value.code == "invalid_value"
    assert marker not in str(raised.value)


@_POSIX_SECRET_SOURCE
def test_value_at_protocol_limit_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = b"x" * 65_525
    monkeypatch.setattr(
        secret_session_module.os,
        "environb",
        {b"CDH_TEST_ROOT_TOKEN": value},
    )
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')

    with HostSecretSession.from_configuration(result) as session:
        assert session.snapshot("root_token").stat().st_size == len(value)


@_POSIX_SECRET_SOURCE
def test_failed_source_outcome_is_cached_without_a_second_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = 0

    class CountingEnvironment(dict[bytes, bytes]):
        def get(self, key: bytes, default=None):
            nonlocal reads
            reads += 1
            return super().get(key, default)

    monkeypatch.setattr(
        secret_session_module.os,
        "environb",
        CountingEnvironment({b"CDH_TEST_ROOT_TOKEN": b""}),
    )
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')

    with HostSecretSession.from_configuration(result) as session:
        for _ in range(2):
            with pytest.raises(HostSecretSessionError) as raised:
                session.snapshot("root_token")
            assert raised.value.code == "invalid_value"
        failure = session.root / "failure-root_token"
        assert failure.read_bytes() == b"invalid_value"
        assert stat.S_IMODE(failure.stat().st_mode) == 0o600

    assert reads == 1


@pytest.mark.parametrize("spelling", ["token", "../token", "{absolute}"])
def test_file_sources_use_lexical_base_and_allow_absolute_or_parent_paths(
    spelling: str,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "configuration"
    token = config_dir / "token" if spelling == "token" else tmp_path / "token"
    token.parent.mkdir(exist_ok=True)
    token.write_bytes(b"file-token")
    authored = os.fspath(token) if spelling == "{absolute}" else spelling
    result = _configuration(tmp_path, source=f'file = "{authored}"')

    with HostSecretSession.from_configuration(result) as session:
        assert session.snapshot("root_token").read_bytes() == b"file-token"


def test_file_source_is_no_follow_and_error_omits_locator(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target-token"
    target.write_bytes(b"synthetic-token")
    config_dir = tmp_path / "configuration"
    config_dir.mkdir()
    link = config_dir / "linked-token"
    link.symlink_to(target)
    result = _configuration(tmp_path, source='file = "linked-token"')

    with (
        HostSecretSession.from_configuration(result) as session,
        pytest.raises(HostSecretSessionError) as raised,
    ):
        session.snapshot("root_token")

    assert raised.value.code == "source_unavailable"
    assert os.fspath(link) not in str(raised.value)
    assert "synthetic-token" not in str(raised.value)


@_POSIX_SECRET_SOURCE
def test_permissive_mode_warning_is_content_free(
    tmp_path: Path,
) -> None:
    token = tmp_path / "configuration" / "token"
    token.parent.mkdir()
    token.write_bytes(b"file-token")
    token.chmod(0o640)
    result = _configuration(tmp_path, source='file = "token"')

    with HostSecretSession.from_configuration(result) as session:
        session.snapshot("root_token")
        warnings = session.drain_warnings()
        assert session.drain_warnings() == ()

    assert len(warnings) == 1
    assert warnings[0].path == ("secrets", "root_token", "file")
    assert warnings[0].code == "secret.permissive_file_mode"
    assert warnings[0].severity is DiagnosticSeverity.WARNING
    assert os.fspath(token) not in warnings[0].message
    assert "file-token" not in warnings[0].message


@_POSIX_SECRET_SOURCE
def test_permissive_mode_warning_survives_invalid_file_value(
    tmp_path: Path,
) -> None:
    token = tmp_path / "configuration" / "token"
    token.parent.mkdir()
    token.write_bytes(b"")
    token.chmod(0o644)
    result = _configuration(tmp_path, source='file = "token"')

    with HostSecretSession.from_configuration(result) as session:
        with pytest.raises(HostSecretSessionError) as raised:
            session.snapshot("root_token")
        warnings = session.drain_warnings()

    assert raised.value.code == "invalid_value"
    assert len(warnings) == 1
    assert warnings[0].code == "secret.permissive_file_mode"
    assert os.fspath(token) not in warnings[0].message


def test_file_source_without_posix_mode_emits_no_permission_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "configuration" / "token"
    token.parent.mkdir()
    token.write_bytes(b"file-token")
    result = _configuration(tmp_path, source='file = "token"')
    original_reader = secret_session_module.read_bounded_regular_absolute_file

    def read_without_permission_evidence(path: str, *, max_bytes: int):
        admitted = original_reader(path, max_bytes=max_bytes)
        return SimpleNamespace(
            data=admitted.data,
            mode=None,
        )

    monkeypatch.setattr(
        secret_session_module,
        "read_bounded_regular_absolute_file",
        read_without_permission_evidence,
    )

    with HostSecretSession.from_configuration(result) as session:
        session.snapshot("root_token")
        warnings = session.drain_warnings()
        assert session.drain_warnings() == ()

    assert warnings == ()


@_POSIX_SECRET_SOURCE
def test_private_session_files_are_exact_modes_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    token = tmp_path / "configuration" / "token"
    token.parent.mkdir()
    token.write_bytes(b"file-token")
    token.chmod(0o644)
    result = _configuration(tmp_path, source='file = "token"')
    previous_umask = os.umask(0o777)
    try:
        # Private modes must remain exact even when the caller's umask removes
        # every requested permission bit.
        with HostSecretSession.from_configuration(result) as session:
            session.snapshot("root_token")
            assert stat.S_IMODE(session.root.stat().st_mode) == 0o700
            private_files = tuple(
                path for path in session.root.iterdir() if path.is_file()
            )
            assert private_files
            assert all(
                stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private_files
            )
    finally:
        os.umask(previous_umask)


def test_helper_selects_before_snapshot_and_preserves_binary_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CDH_TEST_ROOT_TOKEN", raising=False)
    monkeypatch.setenv("CDH_TEST_TEAM_TOKEN", "team-secret")
    result = _configuration(
        tmp_path,
        source='env = "CDH_TEST_ROOT_TOKEN"',
        second_route=True,
    )

    with HostSecretSession.from_configuration(result) as session:
        no_match = _helper(
            session,
            monkeypatch,
            "get",
            b"protocol=https\nhost=other.test\npath=team/repo.git\n",
        )
        mismatch = _helper(
            session,
            monkeypatch,
            "get",
            b"protocol=https\nhost=example.test\npath=team/repo.git\n"
            b"username=root-user\n",
        )
        ignored = _helper(session, monkeypatch, "store", b"malformed\0payload")
        selected = _helper(
            session,
            monkeypatch,
            "get",
            b"protocol=https\nhost=example.test\npath=team/repo.git\n",
        )

        assert no_match[0] == mismatch[0] == ignored[0] == 0
        assert no_match[1] == mismatch[1] == ignored[1] == b""
        assert not (session.root / "snapshot-root_token").exists()
        assert selected == (0, b"username=team-user\npassword=team-secret\n")
        assert (session.root / "snapshot-team_token").read_bytes() == b"team-secret"


def test_helper_selected_missing_source_fails_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CDH_TEST_ROOT_TOKEN", raising=False)
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')

    with HostSecretSession.from_configuration(result) as session:
        first = _helper(
            session,
            monkeypatch,
            "get",
            b"protocol=https\nhost=example.test\npath=repository.git\n",
        )
        monkeypatch.setenv("CDH_TEST_ROOT_TOKEN", "late-secret")
        second = _helper(
            session,
            monkeypatch,
            "get",
            b"protocol=https\nhost=example.test\npath=repository.git\n",
        )

        assert first == second == (1, b"")
        assert (session.root / "failure-root_token").read_bytes() == (
            b"source_unavailable"
        )
        assert not (session.root / "snapshot-root_token").exists()


@_POSIX_SECRET_SOURCE
@pytest.mark.parametrize("raised", [None, RuntimeError, KeyboardInterrupt])
def test_session_collects_warnings_and_cleans_up_for_every_exit(
    raised: type[BaseException] | None,
    tmp_path: Path,
) -> None:
    token = tmp_path / "configuration" / "token"
    token.parent.mkdir()
    token.write_bytes(b"file-token")
    token.chmod(0o644)
    result = _configuration(tmp_path, source='file = "token"')
    session = HostSecretSession.from_configuration(result)
    root: Path | None = None

    try:
        with session:
            root = session.root
            session.snapshot("root_token")
            if raised is not None:
                raise raised("expected exit")
    except (RuntimeError, KeyboardInterrupt):
        pass

    assert root is not None
    assert not root.exists()
    assert len(session.drain_warnings()) == 1


def test_cleanup_failure_preserves_primary_and_records_content_free_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')
    session = HostSecretSession.from_configuration(result)
    original_rmtree = secret_session_module.shutil.rmtree
    root: Path | None = None
    primary = RuntimeError("synthetic primary failure")

    def fail_cleanup(path: Path) -> None:
        raise OSError(f"synthetic cleanup failure at {path}")

    monkeypatch.setattr(secret_session_module.shutil, "rmtree", fail_cleanup)
    try:
        with pytest.raises(RuntimeError) as raised, session:
            root = session.root
            raise primary

        assert raised.value is primary
        warnings = session.drain_warnings()
        assert len(warnings) == 1
        assert warnings[0].path == ("secrets",)
        assert warnings[0].code == "secret.cleanup_failed"
        assert warnings[0].severity is DiagnosticSeverity.WARNING
        assert root is not None
        assert os.fspath(root) not in warnings[0].message
    finally:
        if root is not None:
            original_rmtree(root)


def test_cleanup_failure_without_primary_remains_content_free_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')
    session = HostSecretSession.from_configuration(result)
    original_rmtree = secret_session_module.shutil.rmtree
    root: Path | None = None

    def fail_cleanup(path: Path) -> None:
        raise OSError(f"synthetic cleanup failure at {path}")

    monkeypatch.setattr(secret_session_module.shutil, "rmtree", fail_cleanup)
    try:
        with pytest.raises(HostSecretSessionError) as raised, session:
            root = session.root

        assert raised.value.code == "cleanup_failed"
        assert root is not None
        assert os.fspath(root) not in str(raised.value)
        assert session.drain_warnings() == ()
    finally:
        if root is not None:
            original_rmtree(root)


def test_unreferenced_missing_secret_is_inert_and_no_routes_need_no_binding(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _MINIMAL_CONFIG
        + """
[secrets.unused]
file = "missing-token"
"""
    )
    result = load_validate_config_result(config)

    with HostSecretSession.from_configuration(result) as session:
        assert session.git_binding() is None
        assert tuple(session.root.glob("snapshot-*")) == ()


def test_session_creation_failure_is_content_free_and_cleans_partial_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')
    partial_root: Path | None = None

    def fail_metadata(path: Path, _data: bytes) -> None:
        nonlocal partial_root
        partial_root = path.parent
        raise OSError(f"synthetic failure at {path}")

    monkeypatch.setattr(secret_session_module, "_write_private_file", fail_metadata)

    with (
        pytest.raises(HostSecretSessionError) as raised,
        HostSecretSession.from_configuration(result),
    ):
        pytest.fail("failed metadata creation must not enter the session")

    assert raised.value.code == "session_create_failed"
    assert partial_root is not None
    assert not partial_root.exists()
    assert os.fspath(partial_root) not in str(raised.value)


def test_lock_acquire_failure_closes_descriptor_and_is_content_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')
    marker = "synthetic-sensitive-acquire-marker"
    closed_descriptors: list[int] = []
    original_close = secret_session_module._close_lock_descriptor

    def fail_acquire(_descriptor: int) -> bool:
        raise OSError(marker)

    def record_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        original_close(descriptor)

    with HostSecretSession.from_configuration(result) as session:
        monkeypatch.setattr(
            secret_session_module, "acquire_descriptor_lock", fail_acquire
        )
        monkeypatch.setattr(
            secret_session_module, "_close_lock_descriptor", record_close
        )
        with pytest.raises(HostSecretSessionError) as raised:
            session.snapshot("root_token")

    assert raised.value.code == "snapshot_failed"
    assert marker not in str(raised.value)
    assert len(closed_descriptors) == 1


@pytest.mark.parametrize("failure", ["unlock", "close"])
def test_lock_cleanup_failure_is_content_free_and_attempts_close(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')
    monkeypatch.setenv("CDH_TEST_ROOT_TOKEN", "valid-secret")
    marker = f"synthetic-sensitive-{failure}-marker"
    close_calls = 0
    original_close = secret_session_module._close_lock_descriptor

    def fail_unlock(_descriptor: int) -> None:
        raise OSError(marker)

    def close_descriptor(descriptor: int) -> None:
        nonlocal close_calls
        close_calls += 1
        original_close(descriptor)
        if failure == "close":
            raise OSError(marker)

    with HostSecretSession.from_configuration(result) as session:
        if failure == "unlock":
            monkeypatch.setattr(
                secret_session_module, "release_descriptor_lock", fail_unlock
            )
        monkeypatch.setattr(
            secret_session_module, "_close_lock_descriptor", close_descriptor
        )
        with pytest.raises(HostSecretSessionError) as raised:
            session.snapshot("root_token")
        assert (session.root / "snapshot-root_token").read_bytes() == b"valid-secret"

    assert raised.value.code == "snapshot_failed"
    assert marker not in str(raised.value)
    assert close_calls == 1


def test_snapshot_body_failure_is_cached_and_content_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')
    monkeypatch.setenv("CDH_TEST_ROOT_TOKEN", "synthetic-secret-value")
    marker = "synthetic-sensitive-body-marker"

    def fail_snapshot_write(path: Path, data: bytes) -> None:
        raise OSError(f"{marker}: {path}: {data!r}")

    with HostSecretSession.from_configuration(result) as session:
        monkeypatch.setattr(
            secret_session_module, "_write_snapshot", fail_snapshot_write
        )
        with pytest.raises(HostSecretSessionError) as raised:
            session.snapshot("root_token")
        assert (session.root / "failure-root_token").read_bytes() == b"snapshot_failed"

    assert raised.value.code == "snapshot_failed"
    assert marker not in str(raised.value)
    assert "synthetic-secret-value" not in str(raised.value)


@_POSIX_SECRET_SOURCE
def test_snapshot_body_error_outranks_lock_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _configuration(tmp_path, source='env = "CDH_TEST_ROOT_TOKEN"')
    monkeypatch.setattr(
        secret_session_module.os,
        "environb",
        {b"CDH_TEST_ROOT_TOKEN": b""},
    )
    cleanup_calls: list[str] = []
    original_close = secret_session_module._close_lock_descriptor

    def fail_unlock(_descriptor: int) -> None:
        cleanup_calls.append("unlock")
        raise OSError("synthetic-sensitive-unlock-marker")

    def fail_close(descriptor: int) -> None:
        cleanup_calls.append("close")
        original_close(descriptor)
        raise OSError("synthetic-sensitive-close-marker")

    with HostSecretSession.from_configuration(result) as session:
        monkeypatch.setattr(
            secret_session_module, "release_descriptor_lock", fail_unlock
        )
        monkeypatch.setattr(secret_session_module, "_close_lock_descriptor", fail_close)
        with pytest.raises(HostSecretSessionError) as raised:
            session.snapshot("root_token")

    assert raised.value.code == "invalid_value"
    assert cleanup_calls == ["unlock", "close"]
