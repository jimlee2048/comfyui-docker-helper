"""Live Docker runtime smoke for the cdh container entrypoint."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

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
COMFYUI_NIGHTLY_COMMIT = "77917ed3a6291689e5c2ee8ccbdd6708e85a53a6"


def test_v03_image_entrypoint_readiness_hooks_and_sigterm(tmp_path: Path) -> None:
    """Build and run a minimal image through the real cdh PID 1 entrypoint."""
    _require_command("docker")

    suffix = uuid.uuid4().hex[:12]
    image_tag = os.environ.get(
        "CDH_DOCKER_SMOKE_TAG",
        f"cdh-m6-t3-smoke:{suffix}",
    )
    container_name = os.environ.get(
        "CDH_DOCKER_SMOKE_CONTAINER",
        f"cdh-m6-t3-smoke-{suffix}",
    )
    _validate_smoke_resource_names(image_tag, container_name)
    context_dir = tmp_path / "context"
    hooks_dir = tmp_path / "runtime-hooks"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    config_path = _write_smoke_config(tmp_path)
    _write_runtime_hooks(hooks_dir)

    print(f"smoke image tag: {image_tag}", flush=True)
    print(f"smoke container: {container_name}", flush=True)
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

        docker_run = [
            "docker",
            "run",
            "--detach",
            "--name",
            container_name,
            "--publish",
            "127.0.0.1::8188",
            "--volume",
            f"{output_dir}:/smoke-output",
        ]
        if os.environ.get("CDH_DOCKER_SMOKE_USE_GPU") == "1":
            docker_run.extend(["--gpus", "all"])
        docker_run.append(image_tag)
        container_id = _run_capture(
            docker_run,
            timeout=CONTAINER_STOP_TIMEOUT_SECONDS,
        ).strip()
        print(f"smoke container id: {container_id}", flush=True)

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

        host_port = _mapped_host_port(container_name)
        _wait_for_comfyui_readiness(host_port, container_name)
        hook_log = _wait_for_hook_log(output_dir / "hooks.log")
        assert "post-start" in hook_log
        assert "COMFYUI_PATH=/workspace/ComfyUI" in hook_log

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

        final_hook_log = (output_dir / "hooks.log").read_text(encoding="utf-8")
        assert "stop" in final_hook_log
        logs = _run_capture(
            ["docker", "logs", container_name],
            timeout=30.0,
            check=False,
        )
        assert "Running runtime hook source=baked phase=post-start" in logs
        assert "Running runtime hook source=baked phase=stop" in logs
    finally:
        _remove_container(container_name)
        _remove_image(image_tag)


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
    if not image_tag.startswith("cdh-m6-t3-smoke:"):
        raise AssertionError("CDH_DOCKER_SMOKE_TAG must start with 'cdh-m6-t3-smoke:'")
    if not container_name.startswith("cdh-m6-t3-smoke-"):
        raise AssertionError(
            "CDH_DOCKER_SMOKE_CONTAINER must start with 'cdh-m6-t3-smoke-'"
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
) -> str:
    completed = _run_common(args, timeout=timeout, check=check, capture=True)
    return completed.stdout


def _run_common(
    args: list[str],
    *,
    timeout: float,
    check: bool,
    capture: bool,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {shlex.join(args)}", flush=True)
    completed = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        text=True,
        timeout=timeout,
        check=False,
        capture_output=capture,
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


def _mapped_host_port(container_name: str) -> int:
    output = _run_capture(
        ["docker", "port", container_name, "8188/tcp"],
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
            logs = _run_capture(
                ["docker", "logs", container_name],
                timeout=30.0,
                check=False,
            )
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
    logs = _run_capture(["docker", "logs", container_name], timeout=30.0, check=False)
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
