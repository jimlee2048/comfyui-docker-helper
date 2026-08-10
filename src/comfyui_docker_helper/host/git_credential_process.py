"""Safe process inputs for one host Git credential-helper session."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GitCredentialProcessBinding:
    """Fixed Git configuration and environment without Secret values."""

    config_args: tuple[str, ...]
    environment: Mapping[str, str]
