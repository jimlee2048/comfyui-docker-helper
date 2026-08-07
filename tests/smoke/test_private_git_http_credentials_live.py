"""Opt-in Basic-auth HTTP acceptance for private direct-Git nodes."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest
from tests.smoke.test_private_git_build_access_live import (
    _ALPINE_IMAGE,
    _BUILD_TIMEOUT_SECONDS,
    _DOCKER_ENVIRONMENT_NAMES,
    _SERVICE_READY_TIMEOUT_SECONDS,
    _cleanup,
    _docker,
    _docker_resource_exists,
    _initialize_private_repositories,
    _isolated_git_environment,
    _LiveHarness,
    _LiveHarnessError,
    _read_log_tail,
    _require_isolated_preflight,
)

from comfyui_docker_helper.config.git_credentials import has_password_userinfo

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.network,
    pytest.mark.docker,
    pytest.mark.slow,
]

_GIT_PORT = 8080
_SECRET_ENV = "CDH_TEST_PRIVATE_GIT_TOKEN"
_SECRET_NAME = "private_git"
_USERNAME = "token-user"
_GIT_HTTP_TRACE_ENVIRONMENT_NAMES = (
    "GIT_CURL_VERBOSE",
    "GIT_TRACE_CURL",
    "GIT_TRACE_CURL_NO_DATA",
)


def _start_service(harness: _LiveHarness, token_file: Path) -> str:
    _docker(
        harness.path("docker-config"),
        "test network creation",
        "network",
        "create",
        harness.network,
    )
    repositories = harness.root / "repositories"
    repositories.mkdir()
    server = Path(__file__).with_name("private_git_http_server.py").resolve()
    service_script = """
apk add --no-cache git git-daemon python3 >/dev/null
git config --global --add safe.directory "*"
exec python3 /service/private_git_http_server.py /auth/token
    """.strip()
    _docker(
        harness.path("docker-config"),
        "isolated HTTP Git service startup",
        "run",
        "--detach",
        "--name",
        harness.service_container,
        "--network",
        harness.network,
        "--mount",
        f"type=bind,src={server},dst=/service/private_git_http_server.py,readonly",
        "--mount",
        f"type=bind,src={token_file},dst=/auth/token,readonly",
        "--mount",
        f"type=bind,src={repositories},dst=/srv/git,readonly",
        _ALPINE_IMAGE,
        "/bin/sh",
        "-eu",
        "-c",
        service_script,
    )
    host = _docker(
        harness.path("docker-config"),
        "isolated HTTP Git service address inspection",
        "inspect",
        "--format",
        f'{{{{(index .NetworkSettings.Networks "{harness.network}").IPAddress}}}}',
        harness.service_container,
    ).stdout.strip()
    if not host:
        raise _LiveHarnessError("isolated HTTP Git service has no test-network IPv4")
    return host


def _wait_for_service(host: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + _SERVICE_READY_TIMEOUT_SECONDS
    url = f"http://{host}:{_GIT_PORT}/"
    while time.monotonic() < deadline:
        try:
            opener.open(url, timeout=2)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    raise _LiveHarnessError("isolated HTTP Git service did not become ready")


def _write_config(harness: _LiveHarness, root_url: str) -> None:
    route = root_url.rsplit("/", maxsplit=1)[0] + "/"
    harness.path("config.toml").write_text(
        f"""
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "13.0.3"

[pytorch]
version = "2.12.1"

[comfyui]
version = "0.11.0"
install_cli = false
install_manager = false

[[comfyui.custom_nodes]]
type = "git"
url = "{root_url}"
ref = "HEAD"
target_dir = "private-root-node"

[secrets.{_SECRET_NAME}]
env = "{_SECRET_ENV}"

[[cdh.git.credentials]]
match = "{route}"
username = "{_USERNAME}"
password = {{ secret = "{_SECRET_NAME}" }}

[build]
platforms = ["linux/amd64"]
""".lstrip(),
        encoding="utf-8",
    )


def _build_environment(
    harness: _LiveHarness, token: bytes, service_host: str
) -> dict[str, str]:
    environment = _isolated_git_environment(harness.path("home"))
    for name in _DOCKER_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    for name in _GIT_HTTP_TRACE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    environment.update(
        DOCKER_CONFIG=os.fspath(harness.path("docker-config")),
        HOME=os.fspath(harness.path("home")),
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="",
        SSH_ASKPASS="",
        GIT_TRACE_REDACT="1",
    )
    for name in ("NO_PROXY", "no_proxy"):
        existing = environment.get(name)
        environment[name] = f"{existing},{service_host}" if existing else service_host
    environment[_SECRET_ENV] = token.decode("ascii")
    return environment


def _run_formal_build(
    harness: _LiveHarness,
    token: bytes,
    forbidden: tuple[bytes, ...],
    service_host: str,
) -> str:
    environment = _build_environment(harness, token, service_host)
    environment["BUILDX_BUILDER"] = harness.new_builder(1)
    log_path = harness.path("http-build.log")
    arguments = (
        os.fspath(Path(os.sys.executable).with_name("cdh")),
        "host",
        "build",
        "--file",
        os.fspath(harness.path("config.toml")),
        "--context-dir",
        os.fspath(harness.path("context")),
        "--tag",
        harness.image_tag,
        "--load",
    )
    try:
        with log_path.open("wb") as log:
            completed = subprocess.run(
                arguments,
                check=False,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                timeout=_BUILD_TIMEOUT_SECONDS,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _LiveHarnessError("formal HTTP build could not complete") from error

    _assert_file_omits(log_path, forbidden, "captured cdh/Buildx log")
    if completed.returncode != 0:
        diagnostic = _read_log_tail(log_path)[-8192:].decode("utf-8", errors="replace")
        raise _LiveHarnessError(f"formal HTTP build failed unexpectedly:\n{diagnostic}")
    if not _docker_resource_exists(harness, "image", harness.image_tag):
        raise _LiveHarnessError("formal HTTP build did not load its image")
    return harness.image_tag


def _assert_file_omits(path: Path, forbidden: tuple[bytes, ...], label: str) -> None:
    overlap = b""
    longest = max(map(len, forbidden))
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            candidate = overlap + chunk
            if any(marker in candidate for marker in forbidden):
                raise _LiveHarnessError(f"private source metadata found in {label}")
            overlap = candidate[-(longest - 1) :] if longest > 1 else b""


def _assert_context_omits(harness: _LiveHarness, forbidden: tuple[bytes, ...]) -> None:
    context = harness.path("context")
    for required in ("build-plan.json", "config.lock.toml", ".cdh-rendered"):
        if not (context / required).is_file():
            raise _LiveHarnessError(f"rendered context is missing {required}")
    for candidate in context.rglob("*"):
        if candidate.is_file():
            _assert_file_omits(candidate, forbidden, "rendered context")


def _assert_image_evidence(
    harness: _LiveHarness,
    image: str,
    root_url: str,
    forbidden: tuple[bytes, ...],
) -> None:
    docker_config = harness.path("docker-config")
    inspect = _docker(
        docker_config, "private HTTP image config inspection", "image", "inspect", image
    )
    history = _docker(
        docker_config,
        "private HTTP image history inspection",
        "image",
        "history",
        "--no-trunc",
        image,
    )
    for label, content in (
        ("image config", inspect.stdout),
        ("image history", history.stdout),
    ):
        if any(marker in content.encode() for marker in forbidden):
            raise _LiveHarnessError(f"private source metadata found in {label}")

    node = "/workspace/ComfyUI/custom_nodes/private-root-node"
    submodule = f"{node}/nested/private-submodule"
    repository_check = "\n".join(
        (
            f"test -f {shlex.quote(node + '/__init__.py')}",
            f"test -f {shlex.quote(submodule + '/submodule-marker.txt')}",
            f"test ! -e /run/secrets/cdh-git-credential-{_SECRET_NAME}",
            f"git -C {shlex.quote(node)} remote get-url origin",
            f"git -C {shlex.quote(submodule)} remote get-url origin",
            f"git -C {shlex.quote(node)} config --local --list --show-origin",
            f"git -C {shlex.quote(submodule)} config --local --list --show-origin",
        )
    )
    repository = _docker(
        docker_config,
        "retained private HTTP Git repository inspection",
        "run",
        "--rm",
        "--name",
        harness.residue_container,
        "--entrypoint",
        "/bin/sh",
        image,
        "-eu",
        "-c",
        repository_check,
    )
    repository_output = (repository.stdout + repository.stderr).encode()
    if any(marker in repository_output for marker in forbidden):
        raise _LiveHarnessError("private source metadata found in retained Git config")
    remote_lines = repository.stdout.splitlines()[:2]
    submodule_url = root_url.replace("private-root.git", "private-submodule.git")
    if remote_lines != [root_url, submodule_url] or any(
        has_password_userinfo(url) for url in remote_lines
    ):
        raise _LiveHarnessError("retained Git remotes are not credential-free")

    _docker(
        docker_config,
        "private HTTP image filesystem container creation",
        "container",
        "create",
        "--name",
        harness.residue_container,
        image,
    )
    filesystem = harness.path("final-filesystem.tar")
    _docker(
        docker_config,
        "private HTTP final filesystem export",
        "container",
        "export",
        "--output",
        os.fspath(filesystem),
        harness.residue_container,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    _assert_file_omits(filesystem, forbidden, "final image filesystem")


def test_private_http_git_root_and_recursive_submodule_use_secret_routes() -> None:
    suffix = uuid.uuid4().hex[:12]
    token = f"cdh-synthetic-private-git-token-{suffix}".encode("ascii")
    forbidden = (token, _SECRET_ENV.encode("ascii"))
    root = Path(tempfile.mkdtemp(prefix=f"cdh-private-http-git-{suffix}-"))
    harness = _LiveHarness(
        root=root,
        network=f"cdh-private-http-git-{suffix}-network",
        service_container=f"cdh-private-http-git-{suffix}-git",
        residue_container=f"cdh-private-http-git-{suffix}-residue",
        builders=(f"cdh-private-http-git-{suffix}-builder",),
        image_tag=f"cdh-private-http-git-live:{suffix}",
    )
    try:
        harness.path("docker-config").mkdir()
        _require_isolated_preflight(harness)
        token_file = harness.path("http-token")
        token_file.write_bytes(token)
        token_file.chmod(0o600)
        host = _start_service(harness, token_file)
        _wait_for_service(host)
        root_url = f"http://{host}:{_GIT_PORT}/private-root.git"
        _initialize_private_repositories(harness, root_url)
        _write_config(harness, root_url)

        image = _run_formal_build(harness, token, forbidden, host)

        _assert_context_omits(harness, forbidden)
        _assert_image_evidence(harness, image, root_url, forbidden)
    finally:
        _cleanup(harness)
