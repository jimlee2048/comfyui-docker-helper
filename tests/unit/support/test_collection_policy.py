"""Collection-time safety contracts for the shared pytest plugin."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tests.conftest import _MAX_NODE_ID_CHARACTERS, _validate_node_id_lengths
from tests.project_paths import PROJECT_ROOT


def _item(node_id: str) -> pytest.Item:
    return cast(pytest.Item, SimpleNamespace(nodeid=node_id))


def test_node_id_at_collection_limit_is_accepted() -> None:
    _validate_node_id_lengths([_item("x" * _MAX_NODE_ID_CHARACTERS)])


def test_oversized_node_ids_fail_without_echoing_their_content() -> None:
    marker = "synthetic-sensitive-marker"
    lengths = (_MAX_NODE_ID_CHARACTERS + 1, _MAX_NODE_ID_CHARACTERS + 7)
    items = [_item(marker + "x" * (length - len(marker))) for length in lengths]

    with pytest.raises(pytest.UsageError) as raised:
        _validate_node_id_lengths(items)

    message = str(raised.value)
    assert "2 collected test node ID(s)" in message
    assert f"longest is {max(lengths)} characters" in message
    assert "Use concise explicit parametrization IDs" in message
    assert marker not in message


def test_pytest_rejects_oversized_generated_node_id_before_execution(
    tmp_path: Path,
) -> None:
    marker = "synthetic-sensitive-marker"
    probe = tmp_path / "test_node_id_probe.py"
    probe.write_text(
        "import pytest\n\n"
        f"PAYLOAD = {marker!r} + 'x' * {_MAX_NODE_ID_CHARACTERS}\n\n"
        "@pytest.mark.parametrize('payload', [PAYLOAD])\n"
        "def test_probe(payload):\n"
        "    raise AssertionError(payload)\n"
    )

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-c",
            str(PROJECT_ROOT / "pyproject.toml"),
            "-q",
            str(probe),
        ),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output
    assert "1 collected test node ID(s)" in output
    assert "Use concise explicit parametrization IDs" in output
    assert marker not in output
