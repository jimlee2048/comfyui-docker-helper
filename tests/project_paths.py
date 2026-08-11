"""Stable repository paths for test-owned filesystem contracts."""

from pathlib import Path

TESTS_ROOT = Path(__file__).parent
PROJECT_ROOT = TESTS_ROOT.parent
FIXTURES_ROOT = TESTS_ROOT / "fixtures"
