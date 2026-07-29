"""Opt-in isolated BuildKit/SSH acceptance for private direct-Git nodes."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from comfyui_docker_helper.build_ssh import KNOWN_HOSTS_MOUNTS

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.network,
    pytest.mark.docker,
    pytest.mark.slow,
]

_ALPINE_IMAGE = "alpine:3.22"
_SETUP_TIMEOUT_SECONDS = 180
_BUILD_TIMEOUT_SECONDS = 7200
_SERVICE_READY_TIMEOUT_SECONDS = 120
_DOCKER_ENVIRONMENT_NAMES = (
    "BUILDKIT_HOST",
    "BUILDX_BUILDER",
    "BUILDX_CONFIG",
    "DOCKER_AUTH_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
)


class _LiveHarnessError(RuntimeError):
    pass


# Command wrappers isolate ambient Docker and Git state and bound every subprocess.
def _docker_environment(docker_config: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in _DOCKER_ENVIRONMENT_NAMES:
        env.pop(name, None)
    env["DOCKER_CONFIG"] = os.fspath(docker_config)
    return env


def _run(
    label: str,
    arguments: tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
    timeout: float = _SETUP_TIMEOUT_SECONDS,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _LiveHarnessError(f"{label} could not complete") from error
    if check and completed.returncode != 0:
        raise _LiveHarnessError(f"{label} failed with exit code {completed.returncode}")
    return completed


def _docker(
    docker_config: Path,
    label: str,
    *arguments: str,
    timeout: float = _SETUP_TIMEOUT_SECONDS,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        label,
        ("docker", *arguments),
        env=_docker_environment(docker_config),
        timeout=timeout,
        check=check,
    )


def _isolated_git_environment(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("GIT_CONFIG_") or name in (
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        ):
            env.pop(name, None)
    env.update(
        HOME=os.fspath(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    return env


def _git(
    home: Path,
    label: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return _run(
        label,
        ("git", *arguments),
        env=_isolated_git_environment(home),
    )


# The harness gives every external resource a collision-checked, disposable identity.
@dataclass(slots=True)
class _LiveHarness:
    root: Path
    network: str
    service_container: str
    residue_container: str
    builders: tuple[str, str, str]
    image_tag: str
    cleanup_armed: bool = False
    agent: subprocess.Popen[bytes] | None = None
    agent_socket: Path | None = None

    def path(self, relative: str) -> Path:
        return self.root / relative

    @property
    def builder_containers(self) -> tuple[str, ...]:
        return tuple(f"buildx_buildkit_{builder}0" for builder in self.builders)

    @property
    def builder_volumes(self) -> tuple[str, ...]:
        return tuple(f"buildx_buildkit_{builder}0_state" for builder in self.builders)

    @property
    def docker_resources(self) -> tuple[tuple[str, str], ...]:
        return (
            *(("builder", name) for name in self.builders),
            *(("container", name) for name in self.builder_containers),
            *(("volume", name) for name in self.builder_volumes),
            ("container", self.service_container),
            ("container", self.residue_container),
            ("image", self.image_tag),
            ("network", self.network),
        )

    def new_builder(self, attempt: int) -> str:
        name = self.builders[attempt - 1]
        _docker(
            self.path("docker-config"),
            f"attempt {attempt} builder creation",
            "buildx",
            "create",
            "--name",
            name,
            "--driver",
            "docker-container",
            "--driver-opt",
            f"network={self.network}",
            "--bootstrap",
        )
        return name


# Ephemeral agent and service fixtures exercise SSH without maintainer credentials.
def _start_agent(harness: _LiveHarness, private_key: Path) -> None:
    socket_path = harness.root / "agent.sock"
    try:
        agent = subprocess.Popen(
            ("ssh-agent", "-D", "-a", os.fspath(socket_path)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise _LiveHarnessError("isolated SSH agent could not start") from error
    harness.agent = agent
    harness.agent_socket = socket_path
    deadline = time.monotonic() + 10
    while not socket_path.exists():
        if agent.poll() is not None or time.monotonic() >= deadline:
            raise _LiveHarnessError("isolated SSH agent did not become ready")
        time.sleep(0.05)
    agent_env = {**os.environ, "SSH_AUTH_SOCK": os.fspath(socket_path)}
    agent_env.pop("SSH_AGENT_PID", None)
    _run(
        "ephemeral identity admission",
        ("ssh-add", os.fspath(private_key)),
        env=agent_env,
    )


def _start_git_service(harness: _LiveHarness, authorized_keys: Path) -> str:
    _docker(
        harness.path("docker-config"),
        "test network creation",
        "network",
        "create",
        harness.network,
    )
    repositories = harness.root / "repositories"
    repositories.mkdir()
    service_script = """
apk add --no-cache git openssh-server >/dev/null
adduser -D git
passwd -d git >/dev/null
mkdir -p /run/sshd /home/git/.ssh
chown -R git:git /home/git
su git -c 'git config --global --add safe.directory "*"'
ssh-keygen -A >/dev/null
exec /usr/sbin/sshd -D -e \
  -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no \
  -o PermitRootLogin=no \
  -o AllowUsers=git \
  -o StrictModes=no \
  -o AuthorizedKeysFile=/auth/authorized_keys
    """.strip()
    _docker(
        harness.path("docker-config"),
        "isolated Git service startup",
        "run",
        "--detach",
        "--name",
        harness.service_container,
        "--network",
        harness.network,
        "--mount",
        (f"type=bind,src={authorized_keys},dst=/auth/authorized_keys,readonly"),
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
        "isolated Git service address inspection",
        "inspect",
        "--format",
        (f'{{{{(index .NetworkSettings.Networks "{harness.network}").IPAddress}}}}'),
        harness.service_container,
    ).stdout.strip()
    if not host:
        raise _LiveHarnessError("isolated Git service has no test-network IPv4 address")
    return host


def _wait_for_host_key(host: str) -> str:
    deadline = time.monotonic() + _SERVICE_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        completed = _run(
            "isolated Git service readiness probe",
            ("ssh-keyscan", "-T", "2", "-p", "22", host),
            check=False,
            timeout=5,
        )
        lines = [
            line
            for line in completed.stdout.splitlines()
            if line and not line.startswith("#")
        ]
        if completed.returncode == 0 and lines:
            return "\n".join(lines) + "\n"
        time.sleep(0.25)
    raise _LiveHarnessError("isolated Git service did not become ready")


# Private root and submodule repositories provide exact recursive Git content.
def _initialize_private_repositories(harness: _LiveHarness, root_url: str) -> None:
    git_home = harness.path("home")
    repositories = harness.root / "repositories"
    submodule_work = harness.root / "submodule-work"
    root_work = harness.root / "root-work"
    submodule_bare = repositories / "private-submodule.git"
    root_bare = repositories / "private-root.git"
    _create_test_repository(
        git_home,
        submodule_work,
        "submodule-marker.txt",
        "ephemeral private submodule\n",
        "test: add private submodule marker",
    )
    _git(
        git_home,
        "private repository setup",
        "clone",
        "-q",
        "--bare",
        os.fspath(submodule_work),
        os.fspath(submodule_bare),
    )

    _create_test_repository(
        git_home,
        root_work,
        "__init__.py",
        '"""Ephemeral private custom node."""\n',
        "test: add private custom node",
    )
    submodule_url = root_url.replace("private-root.git", "private-submodule.git")
    root = ("-C", os.fspath(root_work))
    _git(
        git_home,
        "private repository setup",
        *root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        "--name",
        "private-submodule",
        os.fspath(submodule_bare),
        "nested/private-submodule",
    )
    _git(
        git_home,
        "private repository setup",
        *root,
        "config",
        "-f",
        ".gitmodules",
        "submodule.private-submodule.url",
        submodule_url,
    )
    _git(
        git_home,
        "private repository setup",
        *root,
        "add",
        ".gitmodules",
        "nested/private-submodule",
    )
    _git(
        git_home,
        "private repository setup",
        *root,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--amend",
        "-q",
        "--no-edit",
    )
    _git(
        git_home,
        "private repository setup",
        "clone",
        "-q",
        "--bare",
        os.fspath(root_work),
        os.fspath(root_bare),
    )


def _create_test_repository(
    git_home: Path,
    repository: Path,
    filename: str,
    content: str,
    message: str,
) -> None:
    _git(git_home, "private repository setup", "init", "-q", os.fspath(repository))
    root = ("-C", os.fspath(repository))
    for key, value in (
        ("user.name", "cdh live test"),
        ("user.email", "cdh-live@example.invalid"),
    ):
        _git(git_home, "private repository setup", *root, "config", key, value)
    (repository / filename).write_text(content, encoding="utf-8")
    _git(git_home, "private repository setup", *root, "add", filename)
    _git(
        git_home,
        "private repository setup",
        *root,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )


def _write_config(harness: _LiveHarness, root_url: str) -> None:
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

[build]
platforms = ["linux/amd64"]
""".lstrip(),
        encoding="utf-8",
    )


def _host_git_environment(harness: _LiveHarness) -> dict[str, str]:
    if harness.agent_socket is None:
        raise _LiveHarnessError("isolated SSH agent socket is unavailable")
    ssh_command = shlex.join(
        (
            "ssh",
            "-F",
            "none",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "KnownHostsCommand=none",
            "-o",
            f"UserKnownHostsFile={harness.path('host-known-hosts')}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
        )
    )
    env = _isolated_git_environment(harness.path("home"))
    for name in _DOCKER_ENVIRONMENT_NAMES:
        env.pop(name, None)
    env.update(
        DOCKER_CONFIG=os.fspath(harness.path("docker-config")),
        HOME=os.fspath(harness.path("home")),
        SSH_AUTH_SOCK=os.fspath(harness.agent_socket),
        GIT_SSH_COMMAND=ssh_command,
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="",
        SSH_ASKPASS="",
    )
    env.pop("SSH_AGENT_PID", None)
    return env


def _require_host_git_access(harness: _LiveHarness, root_url: str) -> None:
    completed = _run(
        "host-side private Git access probe",
        ("git", "ls-remote", "--exit-code", root_url, "HEAD"),
        env=_host_git_environment(harness),
        check=False,
    )
    if completed.returncode != 0:
        diagnostic = completed.stderr[-4096:]
        raise _LiveHarnessError(
            f"host-side private Git access probe failed:\n{diagnostic}"
        )


# Formal CLI attempts distinguish absent forwarding, rejected trust, and success.
def _run_formal_build(
    harness: _LiveHarness,
    *,
    attempt: int,
    forward_ssh: bool,
    locked: bool,
    expect_success: bool,
) -> str:
    builder = harness.new_builder(attempt)
    image = harness.image_tag
    arguments = [
        os.fspath(Path(os.sys.executable).with_name("cdh")),
        "host",
        "build",
        "--file",
        os.fspath(harness.path("config.toml")),
        "--context-dir",
        os.fspath(harness.path("context")),
        "--tag",
        image,
        "--load",
    ]
    if locked:
        arguments.append("--locked")
    if forward_ssh:
        arguments.append("--ssh")
    env = _host_git_environment(harness)
    env["BUILDX_BUILDER"] = builder
    log_path = harness.root / f"attempt-{attempt}.log"
    try:
        with log_path.open("wb") as log:
            completed = subprocess.run(
                arguments,
                check=False,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=_BUILD_TIMEOUT_SECONDS,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _LiveHarnessError(
            f"formal build attempt {attempt} could not complete"
        ) from error

    tail = _read_log_tail(log_path)
    diagnostic = tail[-8192:].decode("utf-8", errors="replace")
    if expect_success:
        if completed.returncode != 0:
            raise _LiveHarnessError(
                f"formal build attempt {attempt} failed unexpectedly:\n{diagnostic}"
            )
        if not _docker_resource_exists(harness, "image", image):
            raise _LiveHarnessError(
                f"formal build attempt {attempt} did not load its image"
            )
    else:
        if completed.returncode == 0:
            raise _LiveHarnessError(
                f"formal build attempt {attempt} succeeded unexpectedly"
            )
        if b"Docker Buildx failed with exit code" not in tail:
            raise _LiveHarnessError(
                f"formal build attempt {attempt} did not reach Buildx:\n{diagnostic}"
            )
        expected_ssh_diagnostic = (
            b"Host key verification failed."
            if attempt == 1
            else b"REMOTE HOST IDENTIFICATION HAS CHANGED"
        )
        if expected_ssh_diagnostic not in tail:
            raise _LiveHarnessError(
                f"formal build attempt {attempt} did not preserve its SSH diagnostic:\n"
                f"{diagnostic}"
            )
        if _docker_resource_exists(harness, "image", image):
            raise _LiveHarnessError(
                f"failed formal build attempt {attempt} loaded an image"
            )
    return image


def _read_log_tail(path: Path, limit: int = 65536) -> bytes:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        return stream.read()


def _assert_secret_mounts_absent(harness: _LiveHarness, image: str) -> None:
    checks = " && ".join(
        (
            f"test ! -e {shlex.quote(descriptor.target)} "
            f"&& test ! -L {shlex.quote(descriptor.target)}"
        )
        for descriptor in KNOWN_HOSTS_MOUNTS
    )
    _docker(
        harness.path("docker-config"),
        "successful image secret-residue check",
        "run",
        "--rm",
        "--name",
        harness.residue_container,
        "--entrypoint",
        "/bin/sh",
        image,
        "-eu",
        "-c",
        checks,
    )


# Preflight and cleanup inspect only names owned by this isolated harness.
def _docker_resource_exists(
    harness: _LiveHarness,
    kind: str,
    name: str,
) -> bool:
    arguments = {
        "builder": ("buildx", "inspect", name),
        "container": ("container", "inspect", name),
        "image": ("image", "inspect", name),
        "network": ("network", "inspect", name),
        "volume": ("volume", "inspect", name),
    }[kind]
    inspected = _docker(
        harness.path("docker-config"),
        f"{kind} existence inspection",
        *arguments,
        check=False,
    )
    if inspected.returncode == 0:
        return True
    health_arguments = (
        ("buildx", "version") if kind == "builder" else ("info", "--format", "{{.ID}}")
    )
    healthy = _docker(
        harness.path("docker-config"),
        f"{kind} inspection health probe",
        *health_arguments,
        check=False,
    )
    if healthy.returncode != 0:
        raise _LiveHarnessError(f"{kind} existence inspection unavailable")
    return False


def _require_isolated_preflight(harness: _LiveHarness) -> None:
    _require_local_linux_docker(harness)
    collisions = [
        kind
        for kind, name in harness.docker_resources
        if _docker_resource_exists(harness, kind, name)
    ]
    if collisions:
        kinds = ", ".join(sorted(set(collisions)))
        raise _LiveHarnessError(f"harness-owned resource name collision: {kinds}")
    harness.cleanup_armed = True


def _cleanup(harness: _LiveHarness) -> None:
    errors: list[str] = []

    def docker_cleanup(label: str, *arguments: str) -> None:
        try:
            _docker(
                harness.path("docker-config"),
                label,
                *arguments,
                check=False,
            )
        except _LiveHarnessError:
            errors.append(label)

    if harness.cleanup_armed:
        commands = [
            *(
                ("builder cleanup", ("buildx", "rm", "--force", name))
                for name in harness.builders
            ),
            *(
                ("harness container cleanup", ("container", "rm", "--force", name))
                for name in (*harness.builder_containers, harness.residue_container)
            ),
            (
                "Git service cleanup",
                ("container", "rm", "--force", harness.service_container),
            ),
            *(
                ("BuildKit state cleanup", ("volume", "rm", "--force", name))
                for name in harness.builder_volumes
            ),
            ("harness image cleanup", ("image", "rm", "--force", harness.image_tag)),
            ("harness network cleanup", ("network", "rm", harness.network)),
        ]
        for label, arguments in commands:
            docker_cleanup(label, *arguments)

        for kind, name in harness.docker_resources:
            try:
                if _docker_resource_exists(harness, kind, name):
                    errors.append(f"{kind} remains")
            except _LiveHarnessError:
                errors.append(f"{kind} cleanup verification unavailable")

    if harness.agent is not None:
        try:
            harness.agent.terminate()
            harness.agent.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                harness.agent.kill()
                harness.agent.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                errors.append("SSH agent cleanup")
        if harness.agent.poll() is None:
            errors.append("SSH agent remains")
    try:
        shutil.rmtree(harness.root)
    except OSError:
        errors.append("temporary directory cleanup")
    if harness.root.exists():
        errors.append("temporary directory remains")
    if errors:
        values = ", ".join(sorted(set(errors)))
        raise _LiveHarnessError(f"harness-owned cleanup incomplete: {values}")


def _require_local_linux_docker(harness: _LiveHarness) -> None:
    baseline = _docker(
        harness.path("docker-config"),
        "local Docker baseline inspection",
        "info",
        "--format",
        "{{.OSType}} {{.Architecture}}",
    ).stdout.split()
    if baseline not in (["linux", "x86_64"], ["linux", "amd64"]):
        raise _LiveHarnessError(
            "live private-Git acceptance requires local Linux x86_64 Docker"
        )


# The vertical contract proves strict trust, agent opt-in, recursive SSH, and cleanup.
def test_private_git_root_and_recursive_submodule_use_isolated_ssh_inputs() -> None:
    suffix = uuid.uuid4().hex[:12]
    root = Path(tempfile.mkdtemp(prefix=f"cdh-private-git-{suffix}-"))
    harness = _LiveHarness(
        root=root,
        network=f"cdh-private-git-{suffix}-network",
        service_container=f"cdh-private-git-{suffix}-git",
        residue_container=f"cdh-private-git-{suffix}-residue",
        builders=tuple(
            f"cdh-private-git-{suffix}-builder-{attempt}" for attempt in range(1, 4)
        ),
        image_tag=f"cdh-private-git-live:{suffix}",
    )
    try:
        harness.path("docker-config").mkdir()
        _require_isolated_preflight(harness)
        default_known_hosts = harness.path("home/.ssh/known_hosts")
        default_known_hosts.parent.mkdir(parents=True)
        private_key = root / "client-key"
        keygen = (
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            os.fspath(private_key),
        )
        _run("ephemeral identity generation", keygen)
        authorized_keys = root / "authorized_keys"
        authorized_keys.write_bytes(private_key.with_suffix(".pub").read_bytes())
        _start_agent(harness, private_key)

        host = _start_git_service(harness, authorized_keys)
        correct_trust = _wait_for_host_key(host)
        harness.path("host-known-hosts").write_text(correct_trust, encoding="utf-8")
        root_url = f"ssh://git@{host}:22/srv/git/private-root.git"
        _initialize_private_repositories(harness, root_url)
        _require_host_git_access(harness, root_url)
        _write_config(harness, root_url)

        public_key_fields = authorized_keys.read_text(encoding="utf-8").split()
        trust_host = correct_trust.split(maxsplit=1)[0]
        wrong_trust = f"{trust_host} {public_key_fields[0]} {public_key_fields[1]}\n"
        attempts = (
            (1, correct_trust, False, False, False),
            (2, wrong_trust, True, True, False),
            (3, correct_trust, True, True, True),
        )
        image = harness.image_tag
        for attempt, trust, forward_ssh, locked, expect_success in attempts:
            default_known_hosts.write_text(trust, encoding="utf-8")
            image = _run_formal_build(
                harness,
                attempt=attempt,
                forward_ssh=forward_ssh,
                locked=locked,
                expect_success=expect_success,
            )
        _assert_secret_mounts_absent(harness, image)
    finally:
        _cleanup(harness)
