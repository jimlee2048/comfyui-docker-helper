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


# SSH forwarding preserves repeatable secret mappings and omits absent trust inputs.
@pytest.mark.parametrize(
    ("known_hosts_bindings", "expected_secrets"),
    [
        ((), None),
        (
            (
                KnownHostsBinding(
                    secret_id="known-hosts-ordinary",
                    source=Path("/trust/known_hosts"),
                ),
                KnownHostsBinding(
                    secret_id="known-hosts-comma",
                    source=Path("/trust/known,hosts"),
                ),
            ),
            [
                "type=file,id=known-hosts-ordinary,src=/trust/known_hosts",
                'type=file,id=known-hosts-comma,"src=/trust/known,hosts"',
            ],
        ),
    ],
)
def test_buildx_maps_default_ssh_and_known_hosts_bindings(
    known_hosts_bindings: tuple[KnownHostsBinding, ...],
    expected_secrets: list[str] | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        known_hosts_bindings=known_hosts_bindings,
        log=lambda message: None,
    )

    assert calls[0]["ssh"] == "default"
    if expected_secrets is None:
        assert "secrets" not in calls[0]
    else:
        assert calls[0]["secrets"] == expected_secrets


def test_buildx_maps_opaque_cache_specs_without_logging_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    logs: list[str] = []
    cache_from = "type=local,src=/cache source"
    cache_to = "type=registry,ref=example/cache:build,mode=max"
    binding = KnownHostsBinding(
        secret_id="known-hosts",
        source=Path("/trust/known_hosts"),
    )

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
        known_hosts_bindings=(binding,),
        cache_from=cache_from,
        cache_to=cache_to,
        log=logs.append,
    )

    assert calls[0]["ssh"] == "default"
    assert calls[0]["secrets"] == ["type=file,id=known-hosts,src=/trust/known_hosts"]
    assert calls[0]["cache_from"] == cache_from
    assert calls[0]["cache_to"] == cache_to
    assert all(
        cache_from not in message and cache_to not in message for message in logs
    )


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
