"""Shared recording settings, independent of container storage implementation."""

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field

from comfyui_docker_helper.config.model_base import ConfigModel

LogMode = Literal["none", "memory", "file"]
_SIZE_PATTERN = re.compile(r"([0-9]+)(b|[kmg]|[kmg]ib)?", re.IGNORECASE | re.ASCII)
_SIZE_MULTIPLIERS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def normalize_log_size(value: object) -> int:
    """Normalize strict bytes or an explicitly supported binary unit string."""
    if type(value) is int:
        size = value
    elif isinstance(value, str) and (match := _SIZE_PATTERN.fullmatch(value)):
        unit = (match[2] or "").lower()
        if unit.endswith("ib"):
            unit = unit[0]
        size = int(match[1]) * _SIZE_MULTIPLIERS[unit]
    else:
        raise ValueError(
            "must be positive integer bytes or a B, k/m/g, KiB/MiB/GiB size"
        )
    if size <= 0:
        raise ValueError("must be greater than 0")
    return size


def validate_log_directory(value: str) -> str:
    """Admit a dedicated canonical POSIX directory without host filesystem I/O."""
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or value.startswith("//")
        or value == "/"
        or path.as_posix() != value
        or ".." in path.parts
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("must be a canonical absolute POSIX directory other than /")
    return value


LogSize = Annotated[int, BeforeValidator(normalize_log_size)]
LogDirectory = Annotated[str, AfterValidator(validate_log_directory)]


class RuntimeLogSettings(ConfigModel):
    """Immutable controller recording configuration with normalized byte capacity."""

    model_config = ConfigDict(frozen=True)

    mode: LogMode = "file"
    directory: LogDirectory = "/var/log/cdh"
    max_size: LogSize = 20 * 1024**2
    max_files: int = Field(default=5, gt=0)
