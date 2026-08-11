"""Collection-time safety contracts for the shared pytest plugin."""

from __future__ import annotations

import hashlib
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


def _safe_probe_evidence(completed: subprocess.CompletedProcess[str]) -> str:
    output = completed.stdout + completed.stderr
    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
    return (
        "probe=oversized-node-id; "
        f"returncode={completed.returncode}; "
        f"stdout_chars={len(completed.stdout)}; "
        f"stderr_chars={len(completed.stderr)}; "
        f"combined_sha256={digest}"
    )


def test_safe_probe_evidence_omits_raw_output_and_reports_bounded_facts() -> None:
    marker = "synthetic-sensitive-marker"
    stdout = f"private stdout containing {marker}"
    stderr = "private synthetic stderr body"
    completed = subprocess.CompletedProcess(
        args=("pytest",),
        returncode=7,
        stdout=stdout,
        stderr=stderr,
    )

    evidence = _safe_probe_evidence(completed)

    if marker in evidence or stdout in evidence or stderr in evidence:
        pytest.fail("safe probe evidence exposed raw process output", pytrace=False)
    digest = hashlib.sha256((stdout + stderr).encode("utf-8")).hexdigest()
    assert evidence == (
        "probe=oversized-node-id; returncode=7; "
        f"stdout_chars={len(stdout)}; stderr_chars={len(stderr)}; "
        f"combined_sha256={digest}"
    )


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
    evidence = _safe_probe_evidence(completed)
    if completed.returncode != pytest.ExitCode.USAGE_ERROR:
        pytest.fail(f"probe returned an unexpected status; {evidence}", pytrace=False)
    if "1 collected test node ID(s)" not in output:
        pytest.fail(f"probe omitted the oversized count; {evidence}", pytrace=False)
    if "Use concise explicit parametrization IDs" not in output:
        pytest.fail(f"probe omitted the remediation hint; {evidence}", pytrace=False)
    if marker in output:
        pytest.fail(f"probe exposed the sensitive marker; {evidence}", pytrace=False)
