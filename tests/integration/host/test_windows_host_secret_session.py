"""Native Windows evidence for command-scoped host Secret snapshots."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from comfyui_docker_helper.config.service import load_validate_config_result
from comfyui_docker_helper.host.secret_session import HostSecretSession

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows environment and private-state primitives",
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


def _configuration(tmp_path: Path, *, source: str):
    config_dir = tmp_path / "configuration"
    config_dir.mkdir(exist_ok=True)
    config = config_dir / "config.toml"
    config.write_text(
        _MINIMAL_CONFIG
        + f"""
[secrets.root_token]
{source}
""",
        encoding="utf-8",
    )
    return load_validate_config_result(config)


def test_windows_unicode_environment_snapshot_is_private_reused_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = "合成密钥-🔒"
    monkeypatch.setenv("CDH_TEST_WINDOWS_TOKEN", value)
    result = _configuration(
        tmp_path,
        source='env = "CDH_TEST_WINDOWS_TOKEN"',
    )
    root: Path | None = None

    with HostSecretSession.from_configuration(result) as session:
        root = session.root
        first = session.snapshot_git_password("root_token")
        second = session.snapshot_git_password("root_token")

        assert first == second
        assert first.read_bytes() == value.encode("utf-8")
        assert session.drain_warnings() == ()

    assert root is not None
    assert not root.exists()


def test_windows_file_snapshot_preserves_bytes_without_permission_warning(
    tmp_path: Path,
) -> None:
    token = tmp_path / "configuration" / "token"
    token.parent.mkdir()
    token.write_bytes(b"windows-file-secret")
    result = _configuration(tmp_path, source='file = "token"')

    with HostSecretSession.from_configuration(result) as session:
        assert (
            session.snapshot_git_password("root_token").read_bytes()
            == b"windows-file-secret"
        )
        assert session.drain_warnings() == ()
