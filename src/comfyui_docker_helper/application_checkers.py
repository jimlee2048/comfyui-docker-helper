"""Owned paths for materialized application capability checkers."""

from pathlib import Path, PurePosixPath

_PACKAGE_ROOT = Path(__file__).parent

APPLICATION_CHECKER_SOURCE = _PACKAGE_ROOT / "resources" / "application-checker.py"
APPLICATION_CHECKER_CONTEXT_PATH = PurePosixPath("checkers/application.py")
APPLICATION_CHECKER_CONTAINER_PATH = Path("/opt/cdh/build/checkers/application.py")
