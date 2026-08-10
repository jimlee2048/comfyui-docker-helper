"""Safe process inputs for one host Git credential-helper session."""

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PureWindowsPath

_HELPER_MODULE = "comfyui_docker_helper.host.git_credential_helper"
_platform_name = os.name


@dataclass(frozen=True, slots=True)
class GitCredentialProcessBinding:
    """Fixed Git configuration and environment without Secret values."""

    config_args: tuple[str, ...]
    environment: Mapping[str, str]


def git_credential_helper_command(executable: str) -> str:
    """Build one Git-documented shell snippet for the current Python process."""
    value = os.fspath(executable)
    if not value or "\0" in value:
        raise ValueError("Git credential helper executable is invalid")
    if _platform_name == "nt":
        value = PureWindowsPath(value).as_posix()
    return f"!{shlex.quote(value)} -m {_HELPER_MODULE}"
