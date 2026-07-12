"""TOML loading for public configuration."""

import tomllib
from collections.abc import Sequence
from pathlib import Path

from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.merge import merge_toml_documents

type ConfigPath = str | Path


def load_config(path: ConfigPath | Sequence[ConfigPath]) -> FinalConfig:
    """Load, merge, and structurally validate TOML configuration file(s)."""
    documents = []
    for item in _coerce_paths(path):
        with Path(item).open("rb") as config_file:
            documents.append(tomllib.load(config_file))

    document = merge_toml_documents(documents)
    return FinalConfig.model_validate(document)


def _coerce_paths(path: ConfigPath | Sequence[ConfigPath]) -> tuple[ConfigPath, ...]:
    if isinstance(path, (str, Path)):
        return (path,)

    paths = tuple(path)
    if not paths:
        raise ValueError("at least one configuration file is required")
    return paths
