"""Shared FinalConfig test documents and stage diagnostics."""

from pathlib import Path
from typing import Any

from comfyui_docker_helper.config.authored.models import FinalConfig
from comfyui_docker_helper.config.authored.validation.domains import (
    validate_final_config_domains,
)
from comfyui_docker_helper.config.authored.validation.semantics import (
    validate_final_config_semantics,
)
from comfyui_docker_helper.config.diagnostics import Diagnostic


def _document() -> dict[str, Any]:
    return {
        "compute_platform": {"type": "cuda", "cuda": {"version": "13.0.3"}},
        "pytorch": {"version": "2.12.1"},
        "comfyui": {"version": "0.11.0", "install_manager": False},
    }


def _credential_document() -> dict[str, Any]:
    document = _document()
    document["secrets"] = {"private_git": {"env": "CDH_PRIVATE_GIT_TOKEN"}}
    document["cdh"] = {
        "git": {
            "credentials": [
                {
                    "match": "https://github.com/acme/",
                    "username": "x-access-token",
                    "password": {"secret": "private_git"},
                }
            ]
        }
    }
    return document


def _diagnostics(
    config: FinalConfig,
    *,
    build_hooks_dir: Path | None = None,
) -> tuple[Diagnostic, ...]:
    domains = validate_final_config_domains(config, build_hooks_dir=build_hooks_dir)
    return (*domains.diagnostics, *validate_final_config_semantics(config, domains))
