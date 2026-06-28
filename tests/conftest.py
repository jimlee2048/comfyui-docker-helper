"""Shared pytest fixtures."""

import pytest
from typer.testing import CliRunner


@pytest.fixture
def cli_runner() -> CliRunner:
    """Return a CLI runner for command tests."""
    return CliRunner()
