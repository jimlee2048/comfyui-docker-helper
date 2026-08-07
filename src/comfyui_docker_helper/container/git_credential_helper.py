"""Thin image-side adapter for the shared Git credential protocol."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from comfyui_docker_helper.config.git_credentials import (
    GIT_CREDENTIAL_VALUE_MAX_BYTES,
    git_credential_secret_target,
    parse_git_credential_context,
)
from comfyui_docker_helper.container.build_plan_input import (
    MATERIALIZED_BUILD_PLAN_PATH,
    BuildPlanInputAdmission,
)
from comfyui_docker_helper.file_admission import (
    read_bounded_regular_absolute_file,
)
from comfyui_docker_helper.git_credential_protocol import (
    GitCredentialProtocolError,
    GitCredentialRuntimeRoute,
    evaluate_git_credential_request,
    render_git_credential_response,
)

GIT_CREDENTIAL_BUILD_PLAN_DIGEST_ENV = "CDH_GIT_CREDENTIAL_BUILD_PLAN_DIGEST"


def _credential_response(
    operation: str,
    payload: bytes,
    *,
    environment: Mapping[str, str],
    build_plan_path: Path | None = None,
) -> bytes | None:
    if operation != "get":
        return None
    expected_digest = environment.get(GIT_CREDENTIAL_BUILD_PLAN_DIGEST_ENV)
    if not expected_digest:
        raise ValueError("Git credential BuildPlan identity is unavailable")
    routes = BuildPlanInputAdmission.from_path(
        MATERIALIZED_BUILD_PLAN_PATH if build_plan_path is None else build_plan_path,
        expected_build_plan_digest=expected_digest,
    ).git_credential_routes()
    runtime_routes = tuple(
        GitCredentialRuntimeRoute(
            context=parse_git_credential_context(route.match),
            username=route.username.encode("utf-8"),
        )
        for route in routes
    )
    decision = evaluate_git_credential_request(operation, payload, runtime_routes)
    if decision is None:
        return None
    target = git_credential_secret_target(routes[decision.route_index].secret_id)
    password = read_bounded_regular_absolute_file(
        target,
        max_bytes=GIT_CREDENTIAL_VALUE_MAX_BYTES,
    ).data
    return render_git_credential_response(decision, password)


def main() -> int:
    """Serve one helper request without diagnostics or persistence."""
    if len(sys.argv) != 2:
        return 1
    operation = sys.argv[1]
    if operation != "get":
        return 0
    try:
        response = _credential_response(
            operation,
            sys.stdin.buffer.read(),
            environment=os.environ,
        )
        if response is not None:
            sys.stdout.buffer.write(response)
            sys.stdout.buffer.flush()
    except (GitCredentialProtocolError, OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - process adapter
    raise SystemExit(main())
