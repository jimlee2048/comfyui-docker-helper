"""Shared validation for the public shutdown timeout."""

from math import isfinite
from typing import Annotated

from pydantic import BeforeValidator


def validate_shutdown_timeout(value: object) -> int | float:
    """Accept a finite positive number or the exact disable sentinel."""
    if type(value) not in (int, float):
        raise ValueError("must be a finite positive number or -1")
    assert isinstance(value, (int, float))
    if not isfinite(value) or (value != -1 and value <= 0):
        raise ValueError("must be a finite positive number or -1")
    return value


ShutdownTimeout = Annotated[int | float, BeforeValidator(validate_shutdown_timeout)]
