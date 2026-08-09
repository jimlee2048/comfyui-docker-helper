"""Thin private process adapter for the cdh Git credential protocol core."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from comfyui_docker_helper.git_credential_protocol import (
    GitCredentialProtocolError,
    evaluate_git_credential_request,
    render_git_credential_response,
)
from comfyui_docker_helper.host.secret_session import (
    GIT_CREDENTIAL_SESSION_ENV,
    HostSecretSession,
    HostSecretSessionError,
)


def main() -> int:
    """Serve one Git helper operation without writing credentials elsewhere."""
    if len(sys.argv) != 2:
        return 1
    operation = sys.argv[1]
    if operation != "get":
        return 0
    root = os.environ.get(GIT_CREDENTIAL_SESSION_ENV)
    if not root:
        return 1
    try:
        session = HostSecretSession._attach(Path(os.path.abspath(root)))
        routes_with_names = session.helper_routes()
        decision = evaluate_git_credential_request(
            operation,
            sys.stdin.buffer.read(),
            tuple(route for route, _name in routes_with_names),
        )
        if decision is None:
            return 0
        secret_name = routes_with_names[decision.route_index][1]
        password = session.snapshot(secret_name).read_bytes()
        sys.stdout.buffer.write(render_git_credential_response(decision, password))
        sys.stdout.buffer.flush()
    except (
        GitCredentialProtocolError,
        HostSecretSessionError,
        OSError,
        ValueError,
    ):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a helper process
    raise SystemExit(main())
