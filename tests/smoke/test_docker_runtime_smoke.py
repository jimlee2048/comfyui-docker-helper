"""Live Docker runtime smoke for the cdh container entrypoint."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import BaseServer
from typing import ClassVar

import httpx
import pytest

from comfyui_docker_helper.config import (
    ComfyCliVersionCandidate,
    LockOptions,
    RegistryInstallMetadata,
    RegistryVersionCandidate,
    SourceResolvers,
)
from comfyui_docker_helper.host.render_service import prepare_render_context

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.docker,
    pytest.mark.network,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("CDH_RUN_DOCKER_SMOKE") != "1",
        reason="set CDH_RUN_DOCKER_SMOKE=1 to run live Docker runtime smoke",
    ),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
READINESS_TIMEOUT_SECONDS = 300.0
HOOK_TIMEOUT_SECONDS = 60.0
BUILD_TIMEOUT_SECONDS = 3600.0
CONTAINER_STOP_TIMEOUT_SECONDS = 120.0
DOWNLOAD_TIMEOUT_SECONDS = 180.0
SSH_TIMEOUT_SECONDS = 90.0
COMFYUI_NIGHTLY_COMMIT = "77917ed3a6291689e5c2ee8ccbdd6708e85a53a6"
ASYNC_PAYLOAD = b"cdh docker smoke async payload\n" * 1024
ASYNC_TARGET = "models/checkpoints/smoke-async.bin"
SSH_PASSWORD = "cdh-smoke-root-password"
REDACTED_SECRET = "<redacted>"


def test_runtime_entrypoint_async_restart_and_ssh_smoke(tmp_path: Path) -> None:
    """Build a real image and exercise async restart plus both root SSH auth modes."""
    _require_command("docker")
    _require_command("ssh")
    _require_command("ssh-keygen")

    suffix = uuid.uuid4().hex[:12]
    image_tag = os.environ.get(
        "CDH_DOCKER_SMOKE_TAG",
        f"cdh-runtime-entrypoint-smoke:{suffix}",
    )
    container_name = os.environ.get(
        "CDH_DOCKER_SMOKE_CONTAINER",
        f"cdh-runtime-entrypoint-smoke-{suffix}",
    )
    _validate_smoke_resource_names(image_tag, container_name)
    context_dir = tmp_path / "context"
    hooks_dir = tmp_path / "runtime-hooks"
    output_dir = tmp_path / "output"
    runtime_config_dir = tmp_path / "runtime-config"
    runtime_state_dir = tmp_path / "runtime-state"
    model_state_dir = tmp_path / "model-state"
    output_dir.mkdir()
    runtime_config_dir.mkdir()
    runtime_state_dir.mkdir()
    model_state_dir.mkdir()
    config_path = _write_smoke_config(tmp_path)
    _write_runtime_hooks(hooks_dir)
    key_path = tmp_path / "id_ed25519"
    _generate_ssh_keypair(key_path)
    public_key = (tmp_path / "id_ed25519.pub").read_text(encoding="utf-8").strip()

    print(f"smoke image tag: {image_tag}", flush=True)
    print(f"smoke container: {container_name}", flush=True)
    with _SlowAsyncDownloadServer(ASYNC_PAYLOAD) as download_server:
        runtime_config_path = _write_mounted_runtime_config(
            runtime_config_dir,
            url=download_server.url,
            password=SSH_PASSWORD,
            public_key=public_key,
        )
        final_file = model_state_dir / "checkpoints" / "smoke-async.bin"
        state_path = runtime_state_dir / "state.json"
        image_built = False

        try:
            _remove_container(container_name)
            prepare_render_context(
                config_path,
                context_dir,
                scripts_dir=tmp_path / "scripts",
                hooks_dir=hooks_dir,
                resolvers=_smoke_resolvers(),
                lock_options=LockOptions(),
                overwrite=True,
                working_directory=PROJECT_ROOT,
            )
            print(f"rendered build context: {context_dir}", flush=True)
            _run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--load",
                    "-t",
                    image_tag,
                    str(context_dir),
                ],
                timeout=BUILD_TIMEOUT_SECONDS,
            )
            image_built = True

            _start_smoke_container(
                container_name,
                image_tag=image_tag,
                output_dir=output_dir,
                runtime_config_path=runtime_config_path,
                runtime_state_dir=runtime_state_dir,
                model_state_dir=model_state_dir,
            )
            _assert_entrypoint(container_name)
            _wait_for_event(
                download_server.first_chunk_sent,
                "async HTTP server did not receive the first container request",
                container_name=container_name,
            )
            assert not final_file.exists()

            comfyui_port = _mapped_host_port(container_name, 8188)
            _wait_for_comfyui_readiness(comfyui_port, container_name)
            hook_log = _wait_for_hook_log(output_dir / "hooks.log")
            assert "post-start" in hook_log
            assert "COMFYUI_PATH=/workspace/ComfyUI" in hook_log
            assert not final_file.exists()
            _assert_runtime_state_status(state_path, "downloading")
            first_staging = _assert_current_staging_exists(model_state_dir)

            ssh_port = _mapped_host_port(container_name, 22)
            _assert_ssh_public_key_login(ssh_port, key_path)
            _assert_ssh_password_login(ssh_port, tmp_path)

            _run(
                ["docker", "kill", "--signal", "TERM", container_name],
                timeout=30.0,
            )
            exit_code = _run_capture(
                ["docker", "wait", container_name],
                timeout=CONTAINER_STOP_TIMEOUT_SECONDS,
            ).strip()
            print(f"container exit code after SIGTERM: {exit_code}", flush=True)
            assert exit_code == "143"
            assert not final_file.exists()
            _assert_runtime_state_status(state_path, "downloading")
            if first_staging.exists():
                _assert_staging_artifact(first_staging)

            first_logs = _docker_logs(container_name)
            _assert_no_secret_leak(first_logs, "first container logs")
            _assert_log_event(
                first_logs,
                "Async runtime download queue accepted",
                "items=1",
            )
            _assert_log_event(
                first_logs,
                "Runtime download reconcile",
                "mode=async",
            )
            _assert_log_event(first_logs, "Runtime download state persisted")
            _assert_log_event(
                first_logs,
                "target=models/checkpoints/smoke-async.bin",
                "status=downloading",
            )

            _remove_container(container_name)
            stale_staging = _seed_stale_staging_artifact(
                image_tag,
                model_state_dir,
                first_staging,
            )
            download_server.release_first_request()

            _start_smoke_container(
                container_name,
                image_tag=image_tag,
                output_dir=output_dir,
                runtime_config_path=runtime_config_path,
                runtime_state_dir=runtime_state_dir,
                model_state_dir=model_state_dir,
            )
            _wait_for_comfyui_readiness(
                _mapped_host_port(container_name, 8188),
                container_name,
            )
            _wait_for_file_bytes(final_file, ASYNC_PAYLOAD)
            _assert_runtime_state_status(state_path, "completed")
            assert not first_staging.exists()
            assert not stale_staging.exists()
            _assert_ssh_public_key_login(
                _mapped_host_port(container_name, 22),
                key_path,
            )
            _assert_ssh_password_login(_mapped_host_port(container_name, 22), tmp_path)

            _run(
                ["docker", "kill", "--signal", "TERM", container_name],
                timeout=30.0,
            )
            exit_code = _run_capture(
                ["docker", "wait", container_name],
                timeout=CONTAINER_STOP_TIMEOUT_SECONDS,
            ).strip()
            print(f"container exit code after restart SIGTERM: {exit_code}", flush=True)
            assert exit_code == "143"

            final_hook_log = (output_dir / "hooks.log").read_text(encoding="utf-8")
            assert "stop" in final_hook_log
            logs = _docker_logs(container_name)
            _assert_no_secret_leak(logs, "restart container logs")
            _assert_log_event(logs, "Runtime download reconcile", "mode=async")
            _assert_log_event(
                logs,
                "target=models/checkpoints/smoke-async.bin",
                "status=pending",
                "scheduled=true",
            )
            _assert_log_event(
                logs,
                "Runtime download reconciliation persisted",
                "entries=1",
                "async_scheduled=1",
                "async_skipped=0",
            )
            _assert_log_event(logs, "stale_staging=0")
            _assert_log_event(
                logs,
                "Async runtime download queue finished",
                "items=1",
            )
            _assert_log_event(
                logs,
                "target=models/checkpoints/smoke-async.bin",
                "status=completed",
            )
            _assert_log_event(
                logs,
                "Running runtime hook",
                "source=baked",
                "phase=post-start",
            )
            _assert_log_event(
                logs,
                "Running runtime hook",
                "source=baked",
                "phase=stop",
            )
        finally:
            download_server.release_first_request()
            _remove_container(container_name)
            if image_built:
                _restore_tmp_path_ownership(image_tag, tmp_path)
            _remove_image(image_tag)


def _start_smoke_container(
    container_name: str,
    *,
    image_tag: str,
    output_dir: Path,
    runtime_config_path: Path,
    runtime_state_dir: Path,
    model_state_dir: Path,
) -> str:
    _remove_container(container_name)
    docker_run = [
        "docker",
        "run",
        "--detach",
        "--name",
        container_name,
        "--add-host",
        "host.docker.internal:host-gateway",
        "--publish",
        "127.0.0.1::8188",
        "--publish",
        "127.0.0.1::22",
        "--volume",
        f"{output_dir}:/smoke-output",
        "--volume",
        f"{runtime_config_path}:/etc/cdh/runtime/config.toml:ro",
        "--volume",
        f"{runtime_state_dir}:/var/lib/cdh/runtime",
        "--volume",
        f"{model_state_dir}:/workspace/ComfyUI/models",
    ]
    if os.environ.get("CDH_DOCKER_SMOKE_USE_GPU") == "1":
        docker_run.extend(["--gpus", "all"])
    docker_run.append(image_tag)
    container_id = _run_capture(
        docker_run,
        timeout=CONTAINER_STOP_TIMEOUT_SECONDS,
    ).strip()
    print(f"smoke container id: {container_id}", flush=True)
    return container_id


def _assert_entrypoint(container_name: str) -> None:
    entrypoint = _run_capture(
        [
            "docker",
            "inspect",
            "--format",
            "{{.Path}} {{json .Args}}",
            container_name,
        ],
        timeout=30.0,
    ).strip()
    print(f"entrypoint inspect: {entrypoint}", flush=True)
    assert entrypoint == 'cdh ["container","entrypoint"]'


class _SlowAsyncDownloadServer:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.path = f"/{uuid.uuid4().hex}/smoke-async.bin"
        self.first_chunk_sent = threading.Event()
        self._release_first_request = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._requests = 0

    def __enter__(self) -> _SlowAsyncDownloadServer:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CdhSmokeHTTP/1.0"
            owner_ref: ClassVar[_SlowAsyncDownloadServer] = owner

            def do_GET(self) -> None:
                self.owner_ref._handle_get(self)

            def log_message(self, format: str, *args: object) -> None:
                print(f"download server: {format % args}", flush=True)

        self._server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="cdh-smoke-http",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release_first_request()
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10.0)

    @property
    def url(self) -> str:
        server = self._require_server()
        port = server.server_address[1]
        return f"http://host.docker.internal:{port}{self.path}"

    def release_first_request(self) -> None:
        self._release_first_request.set()

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path != self.path:
            handler.send_error(404)
            return

        self._requests += 1
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(self.payload)))
        handler.end_headers()
        if self._requests == 1:
            handler.wfile.write(self.payload[:4096])
            handler.wfile.flush()
            self.first_chunk_sent.set()
            self._release_first_request.wait()
            return
        handler.wfile.write(self.payload)
        handler.wfile.flush()

    def _require_server(self) -> BaseServer:
        if self._server is None:
            raise AssertionError("download server is not running")
        return self._server


@dataclass(frozen=True, slots=True)
class _SafeOutput:
    raw: str
    redacted: str


def _write_mounted_runtime_config(
    root: Path,
    *,
    url: str,
    password: str,
    public_key: str,
) -> Path:
    path = root / "config.toml"
    path.write_text(
        f"""\
[system.ssh]
enable = true
port = 22
password = "{password}"
pub_keys = [{public_key!r}]

[cdh]
default_downloader = "httpx"
default_download_mode = "async"
download_max_attempts = 1
download_failure_policy = "fail"

[cdh.downloader.httpx]
timeout = 300
retries = 0

[[files]]
url = "{url}"
dir = "models/checkpoints"
filename = "smoke-async.bin"
overwrite = false
downloader = "httpx"
download_mode = "async"
""",
        encoding="utf-8",
    )
    return path


def _generate_ssh_keypair(key_path: Path) -> None:
    _run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(key_path),
        ],
        timeout=30.0,
    )
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _assert_ssh_public_key_login(port: int, key_path: Path) -> None:
    output = _run_capture(
        [
            "ssh",
            *_ssh_common_options(port),
            "-i",
            str(key_path),
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "IdentitiesOnly=yes",
            "root@127.0.0.1",
            "printf cdh-smoke-key",
        ],
        timeout=SSH_TIMEOUT_SECONDS,
    )
    assert output == "cdh-smoke-key"


def _assert_ssh_password_login(port: int, tmp_path: Path) -> None:
    askpass = tmp_path / "ssh-askpass.sh"
    askpass.write_text(
        """\
#!/bin/sh
printf '%s\\n' "$CDH_SMOKE_SSH_PASSWORD"
""",
        encoding="utf-8",
    )
    askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    env = {
        **os.environ,
        "DISPLAY": "cdh-smoke:0",
        "SSH_ASKPASS": str(askpass),
        "SSH_ASKPASS_REQUIRE": "force",
        "CDH_SMOKE_SSH_PASSWORD": SSH_PASSWORD,
    }
    output = _run_capture(
        [
            "ssh",
            *_ssh_common_options(port),
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=1",
            "root@127.0.0.1",
            "printf cdh-smoke-password",
        ],
        timeout=SSH_TIMEOUT_SECONDS,
        env=env,
        start_new_session=True,
        redacted_env_keys=frozenset({"CDH_SMOKE_SSH_PASSWORD"}),
    )
    assert output == "cdh-smoke-password"


def _ssh_common_options(port: int) -> list[str]:
    return [
        "-p",
        str(port),
        "-o",
        "BatchMode=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
    ]


def _assert_runtime_state_status(path: Path, status: str) -> None:
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            state = _read_runtime_state(path)
            actual = _runtime_state_entry_status(state, ASYNC_TARGET)
            if actual == status:
                print(f"runtime state status observed: {status}", flush=True)
                return
        time.sleep(1.0)
    actual = path.read_text(encoding="utf-8") if path.exists() else "<missing>"
    raise AssertionError(f"runtime state did not reach {status}: {actual}")


def _read_runtime_state(path: Path) -> object:
    content = path.read_text(encoding="utf-8")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def _runtime_state_entry_status(state: object, target: str) -> str | None:
    if not isinstance(state, dict):
        return None
    downloads = state.get("downloads")
    if not isinstance(downloads, dict):
        return None
    entries = downloads.get("entries")
    if not isinstance(entries, dict):
        return None
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("target") == target and isinstance(entry.get("status"), str):
            return entry["status"]
    return None


def _assert_log_event(logs: str, *tokens: str) -> None:
    if not tokens:
        raise AssertionError("log assertion requires at least one token")
    for line in logs.splitlines():
        if all(token in line for token in tokens):
            return
    raise AssertionError(f"log line with tokens was not found: {tokens!r}")


def _assert_current_staging_exists(model_state_dir: Path) -> Path:
    staging_dir = model_state_dir / "checkpoints" / ".cdh-staging"
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if staging_dir.is_dir():
            candidates = sorted(staging_dir.glob("cdh-*.part*"))
            if candidates:
                staging = candidates[0]
                _assert_staging_artifact(staging)
                print(f"runtime staging artifact observed: {staging.name}", flush=True)
                return staging
        time.sleep(1.0)
    raise AssertionError(f"runtime staging artifact was not created: {staging_dir}")


def _assert_staging_artifact(path: Path) -> None:
    assert path.exists(), f"runtime staging artifact is missing: {path}"
    assert path.parent.name == ".cdh-staging"
    assert re.fullmatch(r"cdh-[0-9a-f]{64}\.part(?:\..+)?", path.name)


def _seed_stale_staging_artifact(
    image_tag: str,
    model_state_dir: Path,
    current_staging: Path,
) -> Path:
    staging_dir = model_state_dir / "checkpoints" / ".cdh-staging"
    digest = "0" * 64
    if current_staging.name.startswith(f"cdh-{digest}."):
        digest = "1" * 64
    stale = staging_dir / f"cdh-{digest}.part"
    container_stale = f"/model-state/checkpoints/.cdh-staging/{stale.name}"
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            "--volume",
            f"{model_state_dir}:/model-state",
            image_tag,
            "-c",
            (
                "mkdir -p /model-state/checkpoints/.cdh-staging "
                f"&& printf stale-smoke-staging > {shlex.quote(container_stale)} "
                f"&& touch -d @0 {shlex.quote(container_stale)}"
            ),
        ],
        timeout=CONTAINER_STOP_TIMEOUT_SECONDS,
    )
    return stale


def _restore_tmp_path_ownership(image_tag: str, tmp_path: Path) -> None:
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            "--volume",
            f"{tmp_path}:/smoke-tmp",
            image_tag,
            "-c",
            (
                f"chown -R {os.getuid()}:{os.getgid()} /smoke-tmp "
                "&& chmod -R u+rwX /smoke-tmp"
            ),
        ],
        timeout=CONTAINER_STOP_TIMEOUT_SECONDS,
        check=False,
    )


def _wait_for_file_bytes(path: Path, expected: bytes) -> None:
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.is_file() and path.read_bytes() == expected:
            print(f"downloaded file completed: {path}", flush=True)
            return
        time.sleep(1.0)
    raise AssertionError(f"downloaded file was not completed: {path}")


def _wait_for_event(
    event: threading.Event,
    message: str,
    *,
    container_name: str,
    timeout: float = 60.0,
) -> None:
    if event.wait(timeout=timeout):
        return
    logs = _redacted_docker_logs(container_name)
    raise AssertionError(f"{message}\nredacted docker logs:\n{logs}")


def _docker_logs(container_name: str) -> str:
    output = _run_capture_safe(
        ["docker", "logs", container_name],
        timeout=30.0,
        check=False,
    )
    return output.raw


def _redacted_docker_logs(container_name: str) -> str:
    return _run_capture_safe(
        ["docker", "logs", container_name],
        timeout=30.0,
        check=False,
    ).redacted


def _assert_no_secret_leak(content: str, description: str) -> None:
    if SSH_PASSWORD in content:
        raise AssertionError(f"{description} leaked SSH password")


def _redact_secrets(content: str) -> str:
    return content.replace(SSH_PASSWORD, REDACTED_SECRET)


def _write_smoke_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"

[comfyui]
version = "nightly"
cli_version = "1.11.1"
install_manager = false
listen = "0.0.0.0"
port = 8188
extra_args = ["--cpu"]
""",
        encoding="utf-8",
    )
    return path


def _validate_smoke_resource_names(image_tag: str, container_name: str) -> None:
    if not image_tag.startswith("cdh-runtime-entrypoint-smoke:"):
        raise AssertionError(
            "CDH_DOCKER_SMOKE_TAG must start with 'cdh-runtime-entrypoint-smoke:'"
        )
    if not container_name.startswith("cdh-runtime-entrypoint-smoke-"):
        raise AssertionError(
            "CDH_DOCKER_SMOKE_CONTAINER must start with 'cdh-runtime-entrypoint-smoke-'"
        )


def _smoke_resolvers() -> SourceResolvers:
    return SourceResolvers(
        comfyui=_SmokeComfyUIProvider(),
        comfy_cli=_SmokeComfyCliProvider(),
        registry=_UnusedRegistryProvider(),
        git=_UnusedGitProvider(),
    )


class _SmokeComfyUIProvider:
    def list_releases(self) -> Sequence[object]:
        raise AssertionError("release resolution is not used by this smoke")

    def get_nightly_commit(self) -> str:
        return COMFYUI_NIGHTLY_COMMIT


class _SmokeComfyCliProvider:
    def list_versions(self) -> Sequence[ComfyCliVersionCandidate]:
        return [ComfyCliVersionCandidate(version="1.11.1")]


class _UnusedRegistryProvider:
    def get_install_metadata(
        self,
        node_id: str,
        version: str | None = None,
    ) -> RegistryInstallMetadata:
        del node_id, version
        raise AssertionError("registry resolution is not used by this smoke")

    def list_versions(self, node_id: str) -> Sequence[RegistryVersionCandidate]:
        del node_id
        raise AssertionError("registry resolution is not used by this smoke")


class _UnusedGitProvider:
    def resolve_default_branch_head(self, url: str) -> str:
        del url
        raise AssertionError("git resolution is not used by this smoke")

    def resolve_ref(self, url: str, ref: str) -> str:
        del url, ref
        raise AssertionError("git resolution is not used by this smoke")


def _write_runtime_hooks(root: Path) -> None:
    post_start = root / "post-start.d"
    stop = root / "stop.d"
    post_start.mkdir(parents=True)
    stop.mkdir()
    (post_start / "10-post-start.sh").write_text(
        """\
set -eu
printf 'post-start PWD=%s COMFYUI_PATH=%s WORKSPACE=%s\\n' \
  "$PWD" "$COMFYUI_PATH" "$WORKSPACE" >> /smoke-output/hooks.log
""",
        encoding="utf-8",
    )
    (stop / "90-stop.sh").write_text(
        """\
set -eu
printf 'stop PWD=%s COMFYUI_PATH=%s WORKSPACE=%s\\n' \
  "$PWD" "$COMFYUI_PATH" "$WORKSPACE" >> /smoke-output/hooks.log
""",
        encoding="utf-8",
    )


def _require_command(command: str) -> None:
    if shutil.which(command) is None:
        pytest.skip(f"{command!r} is required for Docker runtime smoke")


def _run(
    args: list[str],
    *,
    timeout: float,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run_common(args, timeout=timeout, check=check, capture=False)


def _run_capture(
    args: list[str],
    *,
    timeout: float,
    check: bool = True,
    env: dict[str, str] | None = None,
    start_new_session: bool = False,
    redacted_env_keys: frozenset[str] = frozenset(),
) -> str:
    completed = _run_common(
        args,
        timeout=timeout,
        check=check,
        capture=True,
        env=env,
        start_new_session=start_new_session,
        redacted_env_keys=redacted_env_keys,
    )
    return completed.stdout


def _run_capture_safe(
    args: list[str],
    *,
    timeout: float,
    check: bool = True,
) -> _SafeOutput:
    print(f"$ {shlex.join(args)}", flush=True)
    completed = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        text=True,
        timeout=timeout,
        check=False,
        capture_output=True,
    )
    raw = completed.stdout + completed.stderr
    redacted = _redact_secrets(raw)
    if completed.stderr:
        print(_redact_secrets(completed.stderr), end="", flush=True)
    if check and completed.returncode != 0:
        raise AssertionError(
            "command failed with exit code "
            f"{completed.returncode}: {shlex.join(args)}\n"
            f"redacted output:\n{redacted}"
        )
    return _SafeOutput(raw=raw, redacted=redacted)


def _run_common(
    args: list[str],
    *,
    timeout: float,
    check: bool,
    capture: bool,
    env: dict[str, str] | None = None,
    start_new_session: bool = False,
    redacted_env_keys: frozenset[str] = frozenset(),
) -> subprocess.CompletedProcess[str]:
    print(f"$ {shlex.join(args)}", flush=True)
    if redacted_env_keys:
        print(
            "redacted env: " + ", ".join(sorted(redacted_env_keys)),
            flush=True,
        )
    completed = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        timeout=timeout,
        check=False,
        capture_output=capture,
        start_new_session=start_new_session,
    )
    if capture and completed.stdout:
        print(completed.stdout, end="", flush=True)
    if capture and completed.stderr:
        print(completed.stderr, end="", flush=True)
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed with exit code {completed.returncode}: {shlex.join(args)}"
        )
    return completed


def _mapped_host_port(container_name: str, container_port: int) -> int:
    output = _run_capture(
        ["docker", "port", container_name, f"{container_port}/tcp"],
        timeout=30.0,
    ).strip()
    print(f"mapped port: {output}", flush=True)
    match = re.search(r":(?P<port>[0-9]+)\Z", output)
    if match is None:
        raise AssertionError(f"could not parse mapped port from docker port: {output}")
    return int(match.group("port"))


def _wait_for_comfyui_readiness(port: int, container_name: str) -> None:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    last_error = "not probed"
    url = f"http://127.0.0.1:{port}/system_stats"
    while time.monotonic() < deadline:
        if not _container_is_running(container_name):
            logs = _redacted_docker_logs(container_name)
            raise AssertionError(f"container exited before readiness\n{logs}")
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and {"system", "devices"} <= set(payload):
                    print(f"readiness succeeded at {url}", flush=True)
                    return
                last_error = "readiness payload missing system/devices"
            else:
                last_error = f"HTTP {response.status_code}"
        except Exception as error:
            last_error = str(error)
        time.sleep(2.0)
    logs = _redacted_docker_logs(container_name)
    raise AssertionError(f"timed out waiting for readiness: {last_error}\n{logs}")


def _container_is_running(container_name: str) -> bool:
    return (
        _run_capture(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}",
                container_name,
            ],
            timeout=30.0,
            check=False,
        ).strip()
        == "true"
    )


def _wait_for_hook_log(path: Path) -> str:
    deadline = time.monotonic() + HOOK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if "post-start" in content:
                print(f"hook log:\n{content}", end="", flush=True)
                return content
        time.sleep(1.0)
    raise AssertionError(f"post-start hook log was not written: {path}")


def _remove_container(container_name: str) -> None:
    _run(
        ["docker", "rm", "--force", container_name],
        timeout=30.0,
        check=False,
    )


def _remove_image(image_tag: str) -> None:
    _run(
        ["docker", "image", "rm", "--force", image_tag],
        timeout=60.0,
        check=False,
    )
