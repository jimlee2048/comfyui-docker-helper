"""Native Windows evidence for command-scoped host Secret snapshots."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from comfyui_docker_helper.config.diagnostics import DiagnosticSeverity
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
        first = session.snapshot("root_token")
        second = session.snapshot("root_token")

        assert first == second
        assert first.read_bytes() == value.encode("utf-8")
        assert session.drain_warnings() == ()

    assert root is not None
    assert not root.exists()


def test_windows_file_snapshot_reports_permissions_once_without_content(
    tmp_path: Path,
) -> None:
    token = tmp_path / "configuration" / "token"
    token.parent.mkdir()
    token.write_bytes(b"windows-file-secret")
    result = _configuration(tmp_path, source='file = "token"')

    with HostSecretSession.from_configuration(result) as session:
        assert session.snapshot("root_token").read_bytes() == b"windows-file-secret"
        warnings = session.drain_warnings()
        assert session.drain_warnings() == ()

    assert len(warnings) == 1
    assert warnings[0].path == ("secrets", "root_token", "file")
    assert warnings[0].code == "secret.file_permissions_unverifiable"
    assert warnings[0].severity is DiagnosticSeverity.WARNING
    assert os.fspath(token) not in warnings[0].message
    assert "windows-file-secret" not in warnings[0].message
