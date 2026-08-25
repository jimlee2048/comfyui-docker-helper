"""Shared Host diagnostic value contracts."""

import pytest

from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticError


def test_diagnostic_error_requires_diagnostics_and_positive_exit_code() -> None:
    diagnostic = Diagnostic(("python", "version"), "python.invalid", "fix it")
    error = DiagnosticError((diagnostic,), exit_code=3)

    assert error.diagnostics == (diagnostic,)
    assert error.exit_code == 3

    with pytest.raises(ValueError, match="at least one"):
        DiagnosticError(())
    with pytest.raises(ValueError, match="positive"):
        DiagnosticError((diagnostic,), exit_code=0)
