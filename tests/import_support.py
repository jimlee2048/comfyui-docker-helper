"""Package-module discovery and clean subprocess import assertions."""

import subprocess
import sys
from pathlib import Path

import comfyui_docker_helper

_PACKAGE_NAME = comfyui_docker_helper.__name__
_PACKAGE_ROOT = Path(comfyui_docker_helper.__file__).parent
_CONTAINER_ROOT = f"{_PACKAGE_NAME}.container"
_CONTAINER_PREFIX = f"{_CONTAINER_ROOT}."
_CONTAINER_CLI = f"{_CONTAINER_ROOT}.cli"
_INTERPRETER_PROBE_TIMEOUT_SECONDS = 30


def _is_package_module(path: Path) -> bool:
    relative_parent = path.relative_to(_PACKAGE_ROOT).parent
    return all(
        (
            _PACKAGE_ROOT.joinpath(*relative_parent.parts[:depth]) / "__init__.py"
        ).is_file()
        for depth in range(1, len(relative_parent.parts) + 1)
    )


def _module_name(path: Path) -> str:
    relative = path.relative_to(_PACKAGE_ROOT)
    parts = relative.parts[:-1]
    if path.name != "__init__.py":
        parts = (*parts, path.stem)
    return ".".join((_PACKAGE_NAME, *parts))


def package_module_names() -> tuple[str, ...]:
    """Return package modules without importing subpackage initializers."""
    return tuple(
        sorted(
            {
                _module_name(path)
                for path in _PACKAGE_ROOT.rglob("*.py")
                if _is_package_module(path)
            }
        )
    )


def host_module_names() -> tuple[str, ...]:
    """Return modules assigned to the Host generic import selector."""
    return tuple(
        name
        for name in package_module_names()
        if not name.startswith(_CONTAINER_PREFIX) or name == _CONTAINER_CLI
    )


def container_module_names() -> tuple[str, ...]:
    """Return modules assigned to the Linux Container import selector."""
    return tuple(
        name
        for name in package_module_names()
        if name.startswith(_CONTAINER_PREFIX) and name != _CONTAINER_CLI
    )


def assert_clean_subprocess_import(module_name: str) -> None:
    """Import one module in isolation and require a clean process result."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys; importlib.import_module(sys.argv[1])",
            module_name,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=_INTERPRETER_PROBE_TIMEOUT_SECONDS,
    )

    details = (
        f"module={module_name!r}, returncode={result.returncode}, "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert result.returncode == 0, details
    assert result.stdout == "", details
    assert result.stderr == "", details
