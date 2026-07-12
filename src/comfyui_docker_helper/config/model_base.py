"""Shared strict Pydantic boundary for runtime-owned documents."""

from pydantic import BaseModel, ConfigDict


class ConfigModel(BaseModel):
    """Reject unknown fields and coercion at runtime document boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)
