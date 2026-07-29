"""Tests for the narrow public-API Buildx adapter."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from python_on_whales.exceptions import DockerException

from comfyui_docker_helper.host.buildx import (
    BuildxBuildError,
    KnownHostsBinding,
    build_image_with_buildx,
)


# The Buildx mapping block protects cdh-owned domain values without treating
# python-on-whales' literal command line or progress format as product API.
def test_buildx_maps_domain_values_and_fully_drains_live_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    logs: list[str] = []
    drained = False

    def build(context: Path, **kwargs: object):
        nonlocal drained
        calls.append({"context": context, **kwargs})
        yield "first line\n"
        yield "second line\n"
        drained = True

    monkeypatch.setattr(
        "comfyui_docker_helper.host.buildx.DockerClient",
        lambda: SimpleNamespace(buildx=SimpleNamespace(build=build)),
    )

    build_image_with_buildx(
        image_tags=("example/comfy:dev", "example/comfy:latest"),
        output="load",
        context_dir="context with space",
        platforms=("linux/amd64",),
        cwd=tmp_path,
        log=logs.append,
    )

    assert drained is True
    assert calls == [
        {
            "context": (tmp_path / "context with space").resolve(),
            "tags": ["example/comfy:dev", "example/comfy:latest"],
            "load": True,
            "push": False,
            "platforms": ["linux/amd64"],
            "progress": "plain",
            "stream_logs": True,
        }
    ]
    assert logs == [
        "Running Docker Buildx (load) for example/comfy:dev, "
        "example/comfy:latest on linux/amd64",
        "first line",
        "second line",
        "Docker Buildx loaded image tags: example/comfy:dev, example/comfy:latest",
    ]


def test_buildx_maps_default_ssh_and_known_hosts_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def build(context: Path, **kwargs: object):
        calls.append({"context": context, **kwargs})
        yield "progress"

    monkeypatch.setattr(
        "comfyui_docker_helper.host.buildx.DockerClient",
        lambda: SimpleNamespace(buildx=SimpleNamespace(build=build)),
    )

    build_image_with_buildx(
        image_tags=("image:tag",),
        context_dir=tmp_path,
        forward_default_ssh=True,
        known_hosts_bindings=(
            KnownHostsBinding(
                secret_id="known-hosts-ordinary",
                source=Path("/trust/known_hosts"),
            ),
            KnownHostsBinding(
                secret_id="known-hosts-space",
                source=Path("/trust/known hosts"),
            ),
            KnownHostsBinding(
                secret_id="known-hosts-comma",
                source=Path("/trust/known,hosts"),
            ),
        ),
        log=lambda message: None,
    )

    assert calls[0]["ssh"] == "default"
    assert calls[0]["secrets"] == [
        "type=file,id=known-hosts-ordinary,src=/trust/known_hosts",
        "type=file,id=known-hosts-space,src=/trust/known hosts",
        'type=file,id=known-hosts-comma,"src=/trust/known,hosts"',
    ]


def test_buildx_forwards_default_ssh_without_known_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def build(context: Path, **kwargs: object):
        calls.append(kwargs)
        yield "progress"

    monkeypatch.setattr(
        "comfyui_docker_helper.host.buildx.DockerClient",
        lambda: SimpleNamespace(buildx=SimpleNamespace(build=build)),
    )

    build_image_with_buildx(
        image_tags=("image:tag",),
        context_dir=tmp_path,
        forward_default_ssh=True,
        log=lambda message: None,
    )

    assert calls[0]["ssh"] == "default"
    assert "secrets" not in calls[0]


@pytest.mark.parametrize(
    ("output", "load", "push", "completed"),
    [
        ("load", True, False, "Docker Buildx loaded image tags: image:tag"),
        ("push", False, True, "Docker Buildx pushed image tags: image:tag"),
    ],
)
def test_buildx_selects_one_output_mode(
    output: str,
    load: bool,
    push: bool,
    completed: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def build(context: Path, **kwargs: object):
        calls.append(kwargs)
        yield "progress"

    monkeypatch.setattr(
        "comfyui_docker_helper.host.buildx.DockerClient",
        lambda: SimpleNamespace(buildx=SimpleNamespace(build=build)),
    )
    logs: list[str] = []

    build_image_with_buildx(
        image_tags=("image:tag",),
        output=output,  # type: ignore[arg-type]
        context_dir=tmp_path,
        log=logs.append,
    )

    assert calls[0]["load"] is load
    assert calls[0]["push"] is push
    assert logs[-1] == completed


# The failure block keeps transport errors user-facing while preserving caller
# cancellation as control flow owned by the CLI.
def test_buildx_translates_public_api_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs: list[str] = []

    def build(*args: object, **kwargs: object):
        del args, kwargs
        yield "underlying Docker diagnostic\n"
        raise DockerException(["docker", "buildx"], 17)

    monkeypatch.setattr(
        "comfyui_docker_helper.host.buildx.DockerClient",
        lambda: SimpleNamespace(buildx=SimpleNamespace(build=build)),
    )

    with pytest.raises(BuildxBuildError, match="exit code 17"):
        build_image_with_buildx(
            image_tags=("image:tag",),
            context_dir=tmp_path,
            log=logs.append,
        )

    assert logs[-1] == "underlying Docker diagnostic"


@pytest.mark.parametrize(
    ("raised", "message"),
    [
        (
            FileNotFoundError(),
            "Docker was not found; install Docker with Buildx and ensure it is on PATH",
        ),
        (OSError("unavailable"), "Docker Buildx could not be started: unavailable"),
    ],
)
def test_buildx_translates_start_failure(
    raised: OSError,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build(*args: object, **kwargs: object):
        del args, kwargs
        raise raised

    monkeypatch.setattr(
        "comfyui_docker_helper.host.buildx.DockerClient",
        lambda: SimpleNamespace(buildx=SimpleNamespace(build=build)),
    )

    with pytest.raises(BuildxBuildError, match=message):
        build_image_with_buildx(
            image_tags=("image:tag",),
            context_dir=tmp_path,
            log=lambda line: None,
        )


def test_buildx_preserves_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def build(*args: object, **kwargs: object):
        del args, kwargs
        yield "progress"
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "comfyui_docker_helper.host.buildx.DockerClient",
        lambda: SimpleNamespace(buildx=SimpleNamespace(build=build)),
    )

    with pytest.raises(KeyboardInterrupt):
        build_image_with_buildx(
            image_tags=("image:tag",),
            context_dir=tmp_path,
            log=lambda message: None,
        )
