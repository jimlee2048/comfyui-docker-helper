"""Tests for the narrow public-API Buildx adapter."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from python_on_whales.exceptions import DockerException

from comfyui_docker_helper.host.buildx import (
    BuildxBuildError,
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
    def build(*args: object, **kwargs: object):
        del args, kwargs
        yield "progress"
        raise DockerException(["docker", "buildx"], 17)

    monkeypatch.setattr(
        "comfyui_docker_helper.host.buildx.DockerClient",
        lambda: SimpleNamespace(buildx=SimpleNamespace(build=build)),
    )

    with pytest.raises(BuildxBuildError, match="exit code 17"):
        build_image_with_buildx(
            image_tags=("image:tag",),
            context_dir=tmp_path,
            log=lambda message: None,
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
