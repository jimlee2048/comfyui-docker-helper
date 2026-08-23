"""Strict structural validation for authored configuration."""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from comfyui_docker_helper.config.authored.models import FinalConfig
from comfyui_docker_helper.config.authored.validation.result import FinalConfigError
from comfyui_docker_helper.config.diagnostics import Diagnostic


def validate_final_config_structure(document: Mapping[str, Any]) -> FinalConfig:
    """Apply only strict Pydantic ingress validation to a final config document."""
    try:
        return FinalConfig.model_validate(document)
    except ValidationError as error:
        diagnostics = tuple(
            Diagnostic(
                path=tuple(item["loc"]),
                code=f"schema.{item['type']}",
                message=item["msg"],
            )
            for item in error.errors(include_url=False, include_context=False)
        )
        raise FinalConfigError(diagnostics) from error
