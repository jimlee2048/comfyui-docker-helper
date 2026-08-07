"""Shared process-local Git credential configuration policy."""

from __future__ import annotations

from collections.abc import Mapping

_PROMPT_DISABLED_ENVIRONMENT = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
    "SSH_ASKPASS": "",
}


class GitCredentialPolicyError(ValueError):
    """One content-free process-policy admission failure."""


def git_credential_config_args(helper: str) -> tuple[str, ...]:
    """Return fixed command-local config that installs only one helper."""
    return tuple(
        item
        for key, value in _credential_config_entries(helper)
        for item in ("-c", f"{key}={value}")
    )


def git_credential_environment(
    environment: Mapping[str, str],
    *,
    helper: str,
    overlay: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Append fixed credential config and disable every interactive prompt."""
    result = dict(environment)
    raw_count = result.get("GIT_CONFIG_COUNT", "0")
    if not raw_count or not raw_count.isascii() or not raw_count.isdigit():
        raise GitCredentialPolicyError("Git process configuration is invalid")
    try:
        count = int(raw_count, 10)
    except ValueError:
        raise GitCredentialPolicyError("Git process configuration is invalid") from None
    if count < 0:
        raise GitCredentialPolicyError("Git process configuration is invalid")
    entries = _credential_config_entries(helper)
    result["GIT_CONFIG_COUNT"] = str(count + len(entries))
    for offset, (key, value) in enumerate(entries):
        index = count + offset
        result[f"GIT_CONFIG_KEY_{index}"] = key
        result[f"GIT_CONFIG_VALUE_{index}"] = value
    if overlay is not None:
        result.update(overlay)
    result.update(_PROMPT_DISABLED_ENVIRONMENT)
    return result


def noninteractive_git_environment(
    environment: Mapping[str, str],
    *,
    overlay: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment and apply the shared non-interactive policy."""
    result = dict(environment)
    if overlay is not None:
        result.update(overlay)
    result.update(_PROMPT_DISABLED_ENVIRONMENT)
    return result


def _credential_config_entries(helper: str) -> tuple[tuple[str, str], ...]:
    return (
        ("credential.helper", ""),
        ("credential.helper", helper),
        ("credential.useHttpPath", "true"),
        ("credential.interactive", "false"),
    )
