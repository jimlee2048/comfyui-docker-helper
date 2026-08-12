"""Opt-in real Docker acceptance for the cdh lifecycle contract."""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.docker, pytest.mark.slow]

_IMAGE_ENV = "CDH_LIFECYCLE_IMAGE"
_CONTEXT_ENV = "CDH_LIFECYCLE_CONTEXT"
_POLL_INTERVAL_SECONDS = 0.05
_VERIFIED_BINDINGS: set[tuple[Path, str]] = set()


def _image() -> str:
    image = os.environ.get(_IMAGE_ENV)
    if not image:
        pytest.fail(f"lifecycle smoke requires environment input {_IMAGE_ENV}")
    return image


def _formal_context(value: str | Path | None = None) -> Path:
    value = os.environ.get(_CONTEXT_ENV) if value is None else value
    if not value:
        pytest.fail(f"lifecycle smoke requires environment input {_CONTEXT_ENV}")
    try:
        context = Path(value).resolve(strict=True)
    except OSError as error:
        pytest.fail(f"lifecycle smoke context could not be resolved: {error}")
    if not context.is_dir():
        pytest.fail("lifecycle smoke context must resolve to a directory")
    required = (
        context / ".cdh-rendered",
        context / "Dockerfile",
        context / "build-plan.json",
    )
    missing = [
        os.fspath(path) for path in required if path.is_symlink() or not path.is_file()
    ]
    if missing:
        pytest.fail(f"lifecycle smoke requires a formal rendered context: {missing}")
    return context


def _assert_image_context_binding(context: Path | None = None) -> None:
    context = _formal_context() if context is None else _formal_context(context)
    binding = (context, _image())
    if binding in _VERIFIED_BINDINGS:
        return
    for filename in ("build-plan.json",):
        expected = subprocess.run(
            ["sha256sum", os.fspath(context / filename)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        observed = _docker(
            "run",
            "--rm",
            "--entrypoint",
            "sha256sum",
            _image(),
            f"/opt/cdh/build/{filename}",
            timeout=120,
        ).stdout.split()[0]
        assert observed == expected, (
            f"lifecycle image does not match formal context {filename}: "
            f"expected={expected} observed={observed}"
        )
    _VERIFIED_BINDINGS.add(binding)


def _launch_script_path() -> str:
    document = json.loads((_formal_context() / "build-plan.json").read_text())
    return document["runtime"]["launch_command"][1]


def _expected_cdh_argv() -> list[str]:
    document = json.loads((_formal_context() / "build-plan.json").read_text())
    environment = document["toolchain"]["tool_store"]["cdh"]["environment"]
    return [
        f"{environment}/bin/python",
        "/opt/uv/bin/cdh",
        "container",
        "runtime",
        "serve",
    ]


def _docker(
    *arguments: str,
    check: bool = True,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _inspect_container(name: str) -> dict[str, object]:
    return json.loads(_docker("inspect", name).stdout)[0]


def _events(root: Path) -> list[str]:
    path = root / "events.log"
    return [] if not path.exists() else path.read_text().splitlines()


def _wait_for_event(root: Path, expected: str, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if expected in _events(root):
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(f"timed out waiting for {expected!r}; events={_events(root)!r}")


def _wait_for_prefix(root: Path, prefix: str, *, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in _events(root):
            if event.startswith(prefix):
                return event
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(f"timed out waiting for {prefix!r}; events={_events(root)!r}")


def _wait_for_file(path: Path, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(f"timed out waiting for file: {path}")


def _write_hook_tree(root: Path) -> Path:
    hooks = root / "hooks" / "stop.d"
    hooks.mkdir(parents=True)
    (hooks / "10-stop.py").write_text(
        """import os
import signal
import time
from pathlib import Path

events = Path('/evidence/events.log')
def record(value):
    with events.open('a', encoding='utf-8') as stream:
        stream.write(value + '\\n')
        stream.flush()
        os.fsync(stream.fileno())

record(f'hook:start:pid={os.getpid()}:ppid={os.getppid()}:pgid={os.getpgrp()}')
mode = os.environ.get('CDH_LIFECYCLE_HOOK_MODE', 'complete')
if mode == 'hang':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
if mode == 'wait':
    release = Path('/evidence/release-stop-hook')
    while not release.exists():
        time.sleep(0.02)
if mode == 'delay':
    time.sleep(0.4)
record('hook:end')
""",
        encoding="utf-8",
    )
    (hooks / "20-later.py").write_text(
        "from pathlib import Path\n"
        "with Path('/evidence/events.log').open('a') as stream:\n"
        "    stream.write('hook:later\\n')\n",
        encoding="utf-8",
    )
    return hooks.parent


def _write_main(root: Path) -> Path:
    path = root / "main.py"
    path.write_text(
        '''"""Deterministic ComfyUI stand-in for lifecycle acceptance."""
import os
import signal
import time
from pathlib import Path

events = Path("/evidence/events.log")
def record(value):
    with events.open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")
        stream.flush()
        os.fsync(stream.fileno())

def stop(sig, _frame):
    record(f"child:signal:{sig}")
    if os.environ.get("CDH_LIFECYCLE_CHILD_MODE", "graceful") == "graceful":
        record("child:exit:0")
        raise SystemExit(0)

record(f"child:start:pid={os.getpid()}:ppid={os.getppid()}")
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
record("child:ready")
if os.environ.get("CDH_LIFECYCLE_CHILD_MODE") == "natural":
    time.sleep(float(os.environ.get("CDH_LIFECYCLE_NATURAL_DELAY", "0.25")))
    code = int(os.environ.get("CDH_LIFECYCLE_NATURAL_EXIT", "23"))
    record(f"child:natural:{code}")
    raise SystemExit(code)
while True:
    signal.pause()
''',
        encoding="utf-8",
    )
    return path


def _write_generation_main(root: Path) -> Path:
    path = root / "generation-main.py"
    path.write_text(
        '''"""Generation-aware deterministic ComfyUI stand-in."""
import os
import signal
import sys
import time
from pathlib import Path

marker = next(
    value.split("=", 1)[1]
    for value in sys.argv[1:]
    if value.startswith("--generation=")
)
events = Path("/evidence/events.log")
def record(value):
    with events.open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")
        stream.flush()
        os.fsync(stream.fileno())

def stop(sig, _frame):
    record(f"child:signal:{marker}:{sig}")
    record(f"child:exit:{marker}:0")
    raise SystemExit(0)

Path(f"/evidence/child-{marker}.pid").write_text(str(os.getpid()))
record(f"child:start:{marker}:pid={os.getpid()}:ppid={os.getppid()}")
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
record(f"child:ready:{marker}")
index = 0
while True:
    if os.environ.get("CDH_LIFECYCLE_EMIT_LOGS") != "1":
        signal.pause()
        continue
    print(f"runtime-stdout:{marker}:{index}", flush=True)
    print(f"runtime-stderr:{marker}:{index}", file=sys.stderr, flush=True)
    index += 1
    time.sleep(0.1)
''',
        encoding="utf-8",
    )
    return path


def _write_generation_config(path: Path, marker: str) -> Path:
    document = f"""[cdh]
shutdown_timeout = 8

[comfyui]
extra_args = ["--generation={marker}"]
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        stream.write(document)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _write_generation_hooks(
    root: Path,
    marker: str,
    *,
    stop_mode: str = "complete",
    check_old_pid: str | None = None,
) -> Path:
    hooks = root / "generation-hooks"
    pre = hooks / "pre-start.d"
    stop = hooks / "stop.d"
    pre.mkdir(parents=True, exist_ok=True)
    stop.mkdir(parents=True, exist_ok=True)
    check = ""
    success = f"hook:pre:{marker}"
    if check_old_pid is not None:
        check = f"""old_pid = Path("/evidence/child-{check_old_pid}.pid").read_text()
if Path(f"/proc/{{old_pid}}").exists():
    raise SystemExit("old ComfyUI still exists before successor pre-start")
"""
        success = f"hook:pre:{marker}:old-gone"
    (pre / f"10-{marker}.py").write_text(
        f'''import os
from pathlib import Path

events = Path("/evidence/events.log")
{check}with events.open("a", encoding="utf-8") as stream:
    stream.write("{success}\\n")
    stream.flush()
    os.fsync(stream.fileno())
''',
        encoding="utf-8",
    )
    if stop_mode == "wait":
        body = f"""record("hook:stop:{marker}:entered")
release = Path("/evidence/release-stop-{marker}")
while not release.exists():
    time.sleep(0.02)
record("hook:stop:{marker}:released")
"""
    elif stop_mode == "fail":
        body = f"""record("hook:stop:{marker}:failed")
raise SystemExit("synthetic {marker} stop-hook failure")
"""
    else:
        body = f"""record("hook:stop:{marker}:complete")
"""
    (stop / f"10-{marker}.py").write_text(
        f"""import os
import time
from pathlib import Path

events = Path("/evidence/events.log")
def record(value):
    with events.open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")
        stream.flush()
        os.fsync(stream.fileno())

{body}""",
        encoding="utf-8",
    )
    return hooks


def _replace_generation_hooks(
    root: Path,
    marker: str,
    *,
    old_marker: str,
) -> Path:
    hooks = root / "generation-hooks"
    for phase in ("pre-start.d", "stop.d"):
        for path in (hooks / phase).iterdir():
            if path.is_file() or path.is_symlink():
                path.unlink()
    return _write_generation_hooks(
        root,
        marker,
        check_old_pid=old_marker,
    )


def _write_base_runtime_config(root: Path) -> Path:
    path = root / "config.toml"
    path.write_text(
        """[cdh]
shutdown_timeout = 8
""",
        encoding="utf-8",
    )
    return path


def _write_runtime_config(root: Path, *, hanging_url: str) -> Path:
    path = root / "config.toml"
    path.write_text(
        f"""[cdh]
default_downloader = "httpx"
default_download_mode = "async"
download_failure_policy = "fail"
shutdown_timeout = -1

[cdh.downloader.httpx]
timeout = 120

[[files]]
url = "{hanging_url}"
dir = "models"
filename = "pending.bin"
downloader = "httpx"
download_mode = "async"
""",
        encoding="utf-8",
    )
    return path


def _write_cdh_exit_barrier(root: Path) -> Path:
    site = root / "cdh-site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        '''"""Hold only the lifecycle-test cdh interpreter during Python exit."""
import atexit
import os
import sys
import time
from pathlib import Path

if (
    os.environ.get("CDH_LIFECYCLE_EXIT_BARRIER") == "1"
    and sys.argv == ["/opt/uv/bin/cdh", "container", "runtime", "serve"]
):
    def hold_cdh_exit():
        Path("/evidence/cdh-exit-hold").write_text(str(os.getpid()))
        release = Path("/evidence/release-cdh-exit")
        while not release.exists():
            time.sleep(0.02)

    atexit.register(hold_cdh_exit)
''',
        encoding="utf-8",
    )
    return site


@contextmanager
def _container(
    root: Path,
    *,
    environment: Mapping[str, str] | None = None,
    hooks: Path | None = None,
    main_path: Path | None = None,
    runtime_config: Path | None = None,
) -> Iterator[str]:
    _assert_image_context_binding()
    main = _write_main(root) if main_path is None else main_path
    if runtime_config is None:
        runtime_config = _write_base_runtime_config(root)
    name = f"cdh-lifecycle-{uuid.uuid4().hex[:12]}"
    command = [
        "run",
        "--detach",
        "--name",
        name,
        "--add-host",
        "host.docker.internal:host-gateway",
        "--mount",
        f"type=bind,src={root},dst=/evidence",
        "--mount",
        f"type=bind,src={main},dst={_launch_script_path()},readonly",
    ]
    if hooks is not None:
        command.extend(
            ("--mount", f"type=bind,src={hooks},dst=/etc/cdh/runtime/hooks,readonly")
        )
    command.extend(
        (
            "--mount",
            f"type=bind,src={runtime_config},dst=/etc/cdh/runtime/config.toml,readonly",
        )
    )
    for key, value in (environment or {}).items():
        command.extend(("--env", f"{key}={value}"))
    command.append(_image())
    try:
        _docker(*command, timeout=120)
        yield name
    finally:
        _docker("rm", "--force", name, check=False)


def _wait_exit(name: str, *, timeout: float = 15) -> int:
    return int(_docker("wait", name, timeout=timeout).stdout.strip())


def _assert_container_stopped(name: str, expected_exit: int) -> None:
    state = _inspect_container(name)["State"]
    assert isinstance(state, dict)
    assert state["Running"] is False
    assert state["Pid"] == 0
    assert state["ExitCode"] == expected_exit
    assert _docker("top", name, check=False).returncode != 0


def _start_runtime_exec(
    name: str,
    root: Path,
    *arguments: str,
    stdout: int | object = subprocess.PIPE,
    stderr: int | object = subprocess.PIPE,
) -> tuple[subprocess.Popen[str], Path]:
    pid_path = root / f"exec-{uuid.uuid4().hex}.pid"
    script = (
        f"echo $$ > /evidence/{pid_path.name}; "
        f"exec /opt/uv/bin/cdh container runtime {' '.join(arguments)}"
    )
    process = subprocess.Popen(
        ["docker", "exec", name, "sh", "-c", script],
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    _wait_for_file(pid_path)
    return process, pid_path


def _finish_exec(
    process: subprocess.Popen[str],
    *,
    timeout: float = 15,
) -> tuple[str, str]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=3)
        pytest.fail("runtime exec client did not exit before the test deadline")
    return stdout or "", stderr or ""


def _wait_for_status(
    name: str,
    *,
    state: str,
    generation: str,
    operation: str | None,
    timeout: float = 10,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        result = _docker(
            "exec",
            name,
            "/opt/uv/bin/cdh",
            "container",
            "runtime",
            "status",
            "--json",
            check=False,
        )
        if result.returncode == 0:
            last = json.loads(result.stdout)
            if (
                last["state"] == state
                and last["generation"] == generation
                and last["operation"] == operation
            ):
                return last
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(f"timed out waiting for runtime status; last={last!r}")


def _wait_for_text(path: Path, expected: str, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and expected in path.read_text(errors="replace"):
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    observed = "" if not path.exists() else path.read_text(errors="replace")
    pytest.fail(f"timed out waiting for {expected!r}; output={observed!r}")


def _event_fields(event: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in event.split(":") if "=" in part)


class _HangingRequestHandler(socketserver.BaseRequestHandler):
    started: threading.Event
    closed: threading.Event

    def handle(self) -> None:
        self.request.settimeout(0.1)
        request = b""
        while b"\r\n\r\n" not in request:
            try:
                chunk = self.request.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                type(self).closed.set()
                return
            if not chunk:
                return
            request += chunk
        type(self).started.set()
        while True:
            try:
                if not self.request.recv(1):
                    type(self).closed.set()
                    return
            except TimeoutError:
                continue
            except OSError:
                type(self).closed.set()
                return


@contextmanager
def _hanging_http_server() -> Iterator[tuple[str, threading.Event, threading.Event]]:
    handler = type(
        "ObservedHangingRequestHandler",
        (_HangingRequestHandler,),
        {"started": threading.Event(), "closed": threading.Event()},
    )
    server = socketserver.ThreadingTCPServer(("0.0.0.0", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = "host.docker.internal"
        yield (
            f"http://{host}:{server.server_address[1]}/pending",
            handler.started,
            handler.closed,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# The built fixture must preserve the rendered image's explicit signal and
# absolute exec-form entrypoint contract before behavioral probes rely on it.
def test_image_declares_exact_stop_and_entrypoint_contract() -> None:
    _assert_image_context_binding()
    dockerfile = (_formal_context() / "Dockerfile").read_text()
    expected = (
        'ENTRYPOINT ["/usr/bin/tini", "--", "/opt/uv/bin/cdh", '
        '"container", "runtime", "serve"]'
    )
    assert dockerfile.count("STOPSIGNAL SIGTERM") == 1
    assert dockerfile.count(expected) == 1
    config = json.loads(_docker("image", "inspect", _image()).stdout)[0]["Config"]
    assert config["StopSignal"] == "SIGTERM"
    assert config["Entrypoint"] == [
        "/usr/bin/tini",
        "--",
        "/opt/uv/bin/cdh",
        "container",
        "runtime",
        "serve",
    ]


# A formal context is accepted only for the image containing its exact plan and
# manifest bytes; a different rendered context must fail before container use.
def test_image_rejects_different_formal_context(tmp_path: Path) -> None:
    changed = tmp_path / "changed-context"
    shutil.copytree(_formal_context(), changed)
    plan = changed / "build-plan.json"
    plan.write_bytes(plan.read_bytes() + b"\n")

    with pytest.raises(AssertionError, match="does not match formal context"):
        _assert_image_context_binding(changed)


# A manual restart replaces ComfyUI while preserving the container's Tini/cdh spine.
def test_runtime_restart_replaces_full_generation_on_stable_topology(
    tmp_path: Path,
) -> None:
    main = _write_generation_main(tmp_path)
    config = _write_generation_config(tmp_path / "generation.toml", "old")
    hooks = _write_generation_hooks(tmp_path, "old", stop_mode="wait")
    restart: subprocess.Popen[str] | None = None

    with _container(
        tmp_path,
        hooks=hooks,
        main_path=main,
        runtime_config=config,
    ) as name:
        try:
            _wait_for_event(tmp_path, "hook:pre:old")
            old_start = _wait_for_prefix(tmp_path, "child:start:old:")
            _wait_for_event(tmp_path, "child:ready:old")
            old_fields = _event_fields(old_start)
            old_child = old_fields["pid"]
            cdh_pid = old_fields["ppid"]
            tini_pid = _inspect_container(name)["State"]["Pid"]
            assert isinstance(tini_pid, int) and tini_pid > 0
            assert _docker("exec", name, "cat", "/proc/1/comm").stdout.strip() == "tini"
            cdh_status = _docker(
                "exec", name, "cat", f"/proc/{cdh_pid}/status"
            ).stdout.splitlines()
            assert "PPid:\t1" in cdh_status

            restart, _client_pid = _start_runtime_exec(name, tmp_path, "restart")
            _wait_for_event(tmp_path, "hook:stop:old:entered")
            _write_generation_config(config, "new")
            _replace_generation_hooks(tmp_path, "new", old_marker="old")
            (tmp_path / "release-stop-old").write_text("release")

            stdout, stderr = _finish_exec(restart)
            assert restart.returncode == 0
            assert "restart" in stdout.lower()
            assert "completed" in stdout.lower()
            assert "op-1" in stdout
            assert stderr == ""
            new_start = _wait_for_prefix(tmp_path, "child:start:new:")
            _wait_for_event(tmp_path, "child:ready:new")
            new_fields = _event_fields(new_start)
            assert new_fields["pid"] != old_child
            assert new_fields["ppid"] == cdh_pid
            assert _inspect_container(name)["State"]["Pid"] == tini_pid
            assert _docker("exec", name, "cat", "/proc/1/comm").stdout.strip() == "tini"
            cdh_status = _docker(
                "exec", name, "cat", f"/proc/{cdh_pid}/status"
            ).stdout.splitlines()
            assert "PPid:\t1" in cdh_status
            assert (
                _docker(
                    "exec", name, "test", "-e", f"/proc/{old_child}", check=False
                ).returncode
                != 0
            )
            cdh_argv = (
                _docker("exec", name, "cat", f"/proc/{cdh_pid}/cmdline")
                .stdout.rstrip("\0")
                .split("\0")
            )
            assert cdh_argv == _expected_cdh_argv()
            status = _wait_for_status(
                name,
                state="running",
                generation="gen-2",
                operation=None,
            )
            assert status["last_restart"] == {
                "id": "op-1",
                "result": "succeeded",
            }
            _docker("stop", "--time", "8", name, timeout=12)
            assert _wait_exit(name) == 0
        finally:
            if restart is not None and restart.poll() is None:
                restart.terminate()
                _finish_exec(restart, timeout=3)

    events = _events(tmp_path)
    assert events.index("hook:stop:old:released") < events.index("child:signal:old:15")
    assert events.index("child:exit:old:0") < events.index("hook:pre:new:old-gone")
    assert events.index("hook:pre:new:old-gone") < events.index(
        next(item for item in events if item.startswith("child:start:new:"))
    )


# A live log session spans replacement without consuming Docker's primary logs.
def test_runtime_follow_spans_restart_and_docker_logs_remain_complete(
    tmp_path: Path,
) -> None:
    main = _write_generation_main(tmp_path)
    config = _write_generation_config(tmp_path / "generation.toml", "old")
    hooks = _write_generation_hooks(tmp_path, "old", stop_mode="wait")
    follow_stdout = tmp_path / "follow.stdout"
    follow_stderr = tmp_path / "follow.stderr"
    follow: subprocess.Popen[str] | None = None
    restart: subprocess.Popen[str] | None = None

    with (
        follow_stdout.open("w", encoding="utf-8") as stdout_stream,
        follow_stderr.open("w", encoding="utf-8") as stderr_stream,
        _container(
            tmp_path,
            environment={"CDH_LIFECYCLE_EMIT_LOGS": "1"},
            hooks=hooks,
            main_path=main,
            runtime_config=config,
        ) as name,
    ):
        try:
            _wait_for_event(tmp_path, "child:ready:old")
            follow, follower_pid_path = _start_runtime_exec(
                name,
                tmp_path,
                "follow",
                stdout=stdout_stream,
                stderr=stderr_stream,
            )
            _wait_for_text(follow_stdout, "runtime-stdout:old:")
            _wait_for_text(follow_stderr, "runtime-stderr:old:")

            restart, _restart_pid = _start_runtime_exec(name, tmp_path, "restart")
            _wait_for_event(tmp_path, "hook:stop:old:entered")
            _write_generation_config(config, "new")
            _replace_generation_hooks(tmp_path, "new", old_marker="old")
            (tmp_path / "release-stop-old").write_text("release")
            restart_stdout, restart_stderr = _finish_exec(restart)
            assert restart.returncode == 0
            assert "restart" in restart_stdout.lower()
            assert "completed" in restart_stdout.lower()
            assert "op-1" in restart_stdout
            assert restart_stderr == ""

            _wait_for_event(tmp_path, "child:ready:new")
            _wait_for_text(follow_stdout, "runtime-stdout:new:")
            _wait_for_text(follow_stderr, "runtime-stderr:new:")
            logs = _docker("logs", name)
            assert "runtime-stdout:old:" in logs.stdout
            assert "runtime-stdout:new:" in logs.stdout
            assert "runtime-stderr:old:" in logs.stderr
            assert "runtime-stderr:new:" in logs.stderr

            follower_pid = follower_pid_path.read_text().strip()
            _docker("exec", name, "kill", "-INT", follower_pid)
            _finish_exec(follow)
            assert follow.returncode == 130
            status = _wait_for_status(
                name,
                state="running",
                generation="gen-2",
                operation=None,
            )
            assert status["last_restart"] == {
                "id": "op-1",
                "result": "succeeded",
            }
            _docker("stop", "--time", "8", name, timeout=12)
            assert _wait_exit(name) == 0
        finally:
            for process in (follow, restart):
                if process is not None and process.poll() is None:
                    process.terminate()
                    _finish_exec(process, timeout=3)


# Real docker exec SIGINT ends only the accepted client's wait; setup below also
# proves exit 130, continued replacement, status visibility, and clean shutdown.
def test_accepted_restart_client_sigint_does_not_cancel_restart(
    tmp_path: Path,
) -> None:
    main = _write_generation_main(tmp_path)
    config = _write_generation_config(tmp_path / "generation.toml", "old")
    hooks = _write_generation_hooks(tmp_path, "old", stop_mode="wait")
    restart: subprocess.Popen[str] | None = None

    with _container(
        tmp_path,
        hooks=hooks,
        main_path=main,
        runtime_config=config,
    ) as name:
        try:
            _wait_for_event(tmp_path, "child:ready:old")
            restart, client_pid_path = _start_runtime_exec(name, tmp_path, "restart")
            _wait_for_event(tmp_path, "hook:stop:old:entered")
            _wait_for_status(
                name,
                state="restarting",
                generation="gen-1",
                operation="op-1",
            )
            client_pid = client_pid_path.read_text().strip()
            _docker("exec", name, "kill", "-INT", client_pid)
            stdout, stderr = _finish_exec(restart)
            assert restart.returncode == 130
            output = stdout + stderr
            assert "Aborted!" not in output
            assert "restart" in output.lower()
            assert "continues" in output.lower()
            assert "op-1" in output

            _write_generation_config(config, "new")
            _replace_generation_hooks(tmp_path, "new", old_marker="old")
            (tmp_path / "release-stop-old").write_text("release")
            _wait_for_event(tmp_path, "child:ready:new")
            status = _wait_for_status(
                name,
                state="running",
                generation="gen-2",
                operation=None,
            )
            assert status["last_restart"] == {
                "id": "op-1",
                "result": "succeeded",
            }
            _docker("stop", "--time", "8", name, timeout=12)
            assert _wait_exit(name) == 0
        finally:
            if restart is not None and restart.poll() is None:
                restart.terminate()
                _finish_exec(restart, timeout=3)


def test_stop_hook_failure_reports_result_cleans_old_owner_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    main = _write_generation_main(tmp_path)
    config = _write_generation_config(tmp_path / "generation.toml", "old")
    hooks = _write_generation_hooks(tmp_path, "old", stop_mode="fail")
    restart: subprocess.Popen[str] | None = None

    with _container(
        tmp_path,
        hooks=hooks,
        main_path=main,
        runtime_config=config,
    ) as name:
        try:
            _wait_for_event(tmp_path, "child:ready:old")
            restart, _client_pid = _start_runtime_exec(name, tmp_path, "restart")
            stdout, stderr = _finish_exec(restart)
            assert restart.returncode == 1
            assert "Runtime restart op-1 failed" in stdout + stderr
            assert "runtime stop hook failed" in stdout + stderr
            assert _wait_exit(name) == 1
            _assert_container_stopped(name, 1)
            logs = _docker("logs", name)
            assert "runtime stop hook failed" in logs.stderr
            assert "synthetic old stop-hook failure" in logs.stderr
        finally:
            if restart is not None and restart.poll() is None:
                restart.terminate()
                _finish_exec(restart, timeout=3)

    events = _events(tmp_path)
    assert "hook:stop:old:failed" in events
    assert "child:signal:old:15" in events
    assert "child:exit:old:0" in events
    assert events.index("hook:stop:old:failed") < events.index("child:signal:old:15")
    assert sum(item.startswith("child:start:") for item in events) == 1


# A first Docker stop signal must run ordered hooks while the child is alive,
# forward SIGTERM, preserve the graceful child result, and stop the container.
def test_first_signal_completes_graceful_order(tmp_path: Path) -> None:
    hooks = _write_hook_tree(tmp_path)
    with _container(tmp_path, hooks=hooks) as name:
        _wait_for_event(tmp_path, "child:ready")
        pid1 = _inspect_container(name)["State"]["Pid"]
        assert isinstance(pid1, int) and pid1 > 0
        assert _docker("exec", name, "cat", "/proc/1/comm").stdout.strip() == "tini"
        child_start = _wait_for_prefix(tmp_path, "child:start:")
        child_fields = dict(part.split("=", 1) for part in child_start.split(":")[2:])
        cdh_pid = child_fields["ppid"]
        cdh_status = _docker(
            "exec", name, "cat", f"/proc/{cdh_pid}/status"
        ).stdout.splitlines()
        assert "PPid:\t1" in cdh_status
        cdh_argv = (
            _docker("exec", name, "cat", f"/proc/{cdh_pid}/cmdline")
            .stdout.rstrip("\0")
            .split("\0")
        )
        assert cdh_argv == _expected_cdh_argv()
        _docker("stop", "--time", "8", name, timeout=12)
        assert _wait_exit(name) == 0
        _assert_container_stopped(name, 0)

    events = _events(tmp_path)
    hook_start = next(item for item in events if item.startswith("hook:start:"))
    fields = dict(part.split("=", 1) for part in hook_start.split(":")[2:])
    assert fields["pid"] == fields["pgid"]
    assert events == [
        events[0],
        "child:ready",
        hook_start,
        "hook:end",
        "hook:later",
        "child:signal:15",
        "child:exit:0",
    ]
    assert fields["ppid"] == cdh_pid


# A successful startup hook may hand off a background service to the user; a
# paired stop hook provides the explicit graceful-stop contract, while Tini
# independently reaps a short-lived adopted orphan.
def test_paired_hooks_stop_user_service_and_tini_reaps_orphan(
    tmp_path: Path,
) -> None:
    hooks = tmp_path / "hooks"
    pre = hooks / "pre-start.d"
    stop = hooks / "stop.d"
    pre.mkdir(parents=True)
    stop.mkdir(parents=True)
    (tmp_path / "service.py").write_text(
        """import os
import signal
import time
from pathlib import Path

events = Path("/evidence/events.log")
def record(value):
    with events.open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")
        stream.flush()
        os.fsync(stream.fileno())
def stop(_sig, _frame):
    record("service:graceful")
    raise SystemExit(0)
Path("/evidence/service.pid").write_text(str(os.getpid()))
signal.signal(signal.SIGTERM, stop)
record("service:ready")
while True:
    signal.pause()
""",
        encoding="utf-8",
    )
    (tmp_path / "orphan.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        "while not Path('/evidence/release-orphan').exists():\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    (pre / "10-services.py").write_text(
        """import subprocess
import sys
from pathlib import Path

subprocess.Popen([sys.executable, "/evidence/service.py"], start_new_session=True)
orphan = subprocess.Popen(
    [sys.executable, "/evidence/orphan.py"], start_new_session=True
)
Path("/evidence/orphan.pid").write_text(str(orphan.pid))
""",
        encoding="utf-8",
    )
    (stop / "10-stop-service.py").write_text(
        """import os
import signal
import time
from pathlib import Path

pid = int(Path("/evidence/service.pid").read_text())
os.kill(pid, signal.SIGTERM)
deadline = time.monotonic() + 3
while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
    time.sleep(0.02)
if Path(f"/proc/{pid}").exists():
    raise SystemExit("background service did not exit")
Path("/evidence/stop-hook-complete").write_text("complete")
""",
        encoding="utf-8",
    )

    with _container(tmp_path, hooks=hooks) as name:
        _wait_for_event(tmp_path, "child:ready")
        _wait_for_event(tmp_path, "service:ready")
        child_start = _wait_for_prefix(tmp_path, "child:start:")
        child_fields = dict(part.split("=", 1) for part in child_start.split(":")[2:])
        cdh_pid = child_fields["ppid"]
        orphan_pid = int((tmp_path / "orphan.pid").read_text())
        orphan_status = _docker(
            "exec", name, "cat", f"/proc/{orphan_pid}/status"
        ).stdout.splitlines()
        assert "PPid:\t1" in orphan_status
        assert (
            _docker(
                "exec", name, "test", "-e", f"/proc/{cdh_pid}", check=False
            ).returncode
            == 0
        )
        (tmp_path / "release-orphan").write_text("release")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (
                _docker(
                    "exec", name, "test", "-e", f"/proc/{orphan_pid}", check=False
                ).returncode
                != 0
            ):
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
        else:
            pytest.fail("Tini did not reap the adopted short-lived orphan")
        _docker("stop", "--time", "8", name, timeout=12)
        assert _wait_exit(name) == 0

    events = _events(tmp_path)
    assert "service:graceful" in events
    assert (tmp_path / "stop-hook-complete").read_text() == "complete"
    assert events.index("service:graceful") < events.index("child:signal:15")


# A repeated signal force-kills managed work; a test-only interpreter exit hold
# proves the direct child is gone while cdh and Tini are still alive.
def test_repeated_signal_reaps_child_before_cdh_exit(tmp_path: Path) -> None:
    hooks = _write_hook_tree(tmp_path)
    _write_cdh_exit_barrier(tmp_path)
    restart: subprocess.Popen[str] | None = None
    with _container(
        tmp_path,
        hooks=hooks,
        environment={
            "CDH_LIFECYCLE_CHILD_MODE": "stubborn",
            "CDH_LIFECYCLE_HOOK_MODE": "wait",
            "CDH_SHUTDOWN_TIMEOUT": "30",
            "CDH_LIFECYCLE_EXIT_BARRIER": "1",
            "PYTHONPATH": "/evidence/cdh-site",
        },
    ) as name:
        try:
            _wait_for_event(tmp_path, "child:ready")
            child_start = _wait_for_prefix(tmp_path, "child:start:")
            child_fields = dict(
                part.split("=", 1) for part in child_start.split(":")[2:]
            )
            child_pid = child_fields["pid"]
            cdh_pid = child_fields["ppid"]
            restart, _client_pid = _start_runtime_exec(name, tmp_path, "restart")
            _wait_for_prefix(tmp_path, "hook:start:")
            started = time.monotonic()
            _docker("kill", "--signal", "SIGINT", name)
            (tmp_path / "release-stop-hook").write_text("release")
            _wait_for_event(tmp_path, "child:signal:2")
            _docker("kill", "--signal", "SIGTERM", name)
            _wait_for_file(tmp_path / "cdh-exit-hold")
            assert (tmp_path / "cdh-exit-hold").read_text() == cdh_pid
            assert _docker("exec", name, "cat", "/proc/1/comm").stdout.strip() == "tini"
            assert (
                _docker(
                    "exec", name, "test", "-e", f"/proc/{cdh_pid}", check=False
                ).returncode
                == 0
            )
            assert (
                _docker(
                    "exec", name, "test", "-e", f"/proc/{child_pid}", check=False
                ).returncode
                != 0
            )
            (tmp_path / "release-cdh-exit").write_text("release")
            assert _wait_exit(name) == 137
            assert time.monotonic() - started < 3
            _assert_container_stopped(name, 137)
            _finish_exec(restart, timeout=3)
        finally:
            if restart is not None and restart.poll() is None:
                restart.terminate()
                _finish_exec(restart, timeout=3)

    events = _events(tmp_path)
    assert events.index("hook:end") < events.index("hook:later")
    assert events.index("hook:later") < events.index("child:signal:2")
    assert sum(item.startswith("child:start:") for item in events) == 1


# A short finite cdh deadline must cut an active hook, reserve time for the
# child, and force a child that ignores the forwarded first signal.
def test_short_outer_deadline_bounds_force_and_exit(tmp_path: Path) -> None:
    hooks = _write_hook_tree(tmp_path)
    with _container(
        tmp_path,
        hooks=hooks,
        environment={
            "CDH_LIFECYCLE_CHILD_MODE": "stubborn",
            "CDH_LIFECYCLE_HOOK_MODE": "hang",
            "CDH_SHUTDOWN_TIMEOUT": "2.5",
        },
    ) as name:
        _wait_for_event(tmp_path, "child:ready")
        started = time.monotonic()
        _docker("stop", "--time", "8", name, timeout=12)
        elapsed = time.monotonic() - started
        assert _wait_exit(name) == 137
        assert 2 <= elapsed < 5
        _assert_container_stopped(name, 137)

    events = _events(tmp_path)
    assert any(item.startswith("hook:start:") for item in events)
    assert "hook:end" not in events
    assert "hook:later" not in events
    assert events.count("child:signal:15") == 1


# Disabling the outer/hook deadline must not disable the downloader's bounded
# cancellation: a live HTTPX request closes while the delayed hook completes.
def test_minus_one_retains_component_cancellation_bound(tmp_path: Path) -> None:
    hooks = _write_hook_tree(tmp_path)
    with _hanging_http_server() as (url, request_started, request_closed):
        runtime_config = _write_runtime_config(tmp_path, hanging_url=url)
        with _container(
            tmp_path,
            hooks=hooks,
            runtime_config=runtime_config,
            environment={"CDH_LIFECYCLE_HOOK_MODE": "delay"},
        ) as name:
            _wait_for_event(tmp_path, "child:ready")
            assert request_started.wait(5)
            started = time.monotonic()
            _docker("stop", "--time", "8", name, timeout=12)
            assert _wait_exit(name) == 0
            assert time.monotonic() - started < 5
            assert request_closed.wait(2)
            _assert_container_stopped(name, 0)

    events = _events(tmp_path)
    assert "hook:end" in events
    assert "hook:later" in events
    assert events[-2:] == ["child:signal:15", "child:exit:0"]


# Natural child exit must preserve its nonzero result and perform ordinary
# cleanup without creating a signal timeline or running signal-only hooks.
def test_natural_exit_preserves_result_without_stop_hooks(tmp_path: Path) -> None:
    hooks = _write_hook_tree(tmp_path)
    with _container(
        tmp_path,
        hooks=hooks,
        environment={
            "CDH_LIFECYCLE_CHILD_MODE": "natural",
            "CDH_LIFECYCLE_NATURAL_EXIT": "23",
        },
    ) as name:
        assert _wait_exit(name) == 23
        _assert_container_stopped(name, 23)

    events = _events(tmp_path)
    assert events[-1] == "child:natural:23"
    assert not any(item.startswith("hook:") for item in events)
    assert not any(item.startswith("child:signal:") for item in events)
