"""Shared strict Pydantic boundary for configuration documents."""

from pydantic import BaseModel, ConfigDict


class ConfigModel(BaseModel):
    """Reject unknown fields and coercion at configuration boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)
