"""Tests for host-side Docker Buildx invocation."""

import subprocess
from pathlib import Path

import pytest

from comfyui_docker_helper.host.buildx import (
    BuildxBuildError,
    build_image_with_buildx,
)


# Buildx ordering tests lock down the exact Docker argv and log handoff.
def test_buildx_invocation_uses_required_argv_and_cwd(tmp_path: Path) -> None:
    """Call exactly docker buildx build --load with repeated tags and no fallback."""
    calls: list[tuple[tuple[str, ...], Path | None, bool]] = []
    logs: list[str] = []

    def runner(
        args: tuple[str, ...],
        *,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[object]:
        calls.append((args, cwd, check))
        return subprocess.CompletedProcess(args, 0)

    result = build_image_with_buildx(
        image_tags=("example/comfy:dev", "example/comfy:latest"),
        context_dir=tmp_path / "context with space",
        cwd=tmp_path,
        runner=runner,
        log=logs.append,
    )

    assert calls == [
        (
            (
                "docker",
                "buildx",
                "build",
                "--load",
                "-t",
                "example/comfy:dev",
                "-t",
                "example/comfy:latest",
                str(tmp_path / "context with space"),
            ),
            tmp_path,
            False,
        )
    ]
    assert result.argv == calls[0][0]
    assert result.context_dir == tmp_path / "context with space"
    assert result.image_tags == ("example/comfy:dev", "example/comfy:latest")
    assert result.output == "load"
    assert logs == [
        "Running Docker Buildx: docker buildx build --load -t "
        "example/comfy:dev -t example/comfy:latest "
        f"'{tmp_path / 'context with space'}'",
        "Docker Buildx loaded image tags: example/comfy:dev, example/comfy:latest",
    ]


def test_buildx_invocation_can_push_tags(tmp_path: Path) -> None:
    """Use --push instead of --load when requested."""
    calls: list[tuple[str, ...]] = []
    logs: list[str] = []

    def runner(
        args: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[object]:
        del kwargs
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    result = build_image_with_buildx(
        image_tags=("registry.example.com/comfy:dev",),
        output="push",
        context_dir=tmp_path,
        runner=runner,
        log=logs.append,
    )

    assert calls == [
        (
            "docker",
            "buildx",
            "build",
            "--push",
            "-t",
            "registry.example.com/comfy:dev",
            str(tmp_path),
        )
    ]
    assert result.output == "push"
    assert logs[-1] == (
        "Docker Buildx pushed image tags: registry.example.com/comfy:dev"
    )


def test_buildx_invocation_uses_inherited_output_streams(tmp_path: Path) -> None:
    """Do not capture stdout/stderr so Docker diagnostics stream directly."""

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        assert "capture_output" not in kwargs
        assert "stdout" not in kwargs
        assert "stderr" not in kwargs
        return subprocess.CompletedProcess(args, 0)

    build_image_with_buildx(
        image_tags=("image:tag",),
        context_dir=tmp_path,
        runner=runner,
        log=lambda message: None,
    )


# Buildx failure tests keep process startup/exit errors user-facing without
# swallowing explicit user interruption.
def test_buildx_missing_docker_is_user_facing(tmp_path: Path) -> None:
    """Report a missing Docker executable as a user-facing build failure."""

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        raise FileNotFoundError("docker")

    with pytest.raises(BuildxBuildError, match="executable was not found"):
        build_image_with_buildx(
            image_tags=("image:tag",),
            context_dir=tmp_path,
            runner=runner,
            log=lambda message: None,
        )


def test_buildx_nonzero_exit_is_user_facing(tmp_path: Path) -> None:
    """Report a nonzero Docker exit code without raising CalledProcessError."""

    def runner(
        args: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[object]:
        return subprocess.CompletedProcess(args, 17)

    with pytest.raises(BuildxBuildError, match="exit code 17"):
        build_image_with_buildx(
            image_tags=("image:tag",),
            context_dir=tmp_path,
            runner=runner,
            log=lambda message: None,
        )


def test_buildx_start_os_error_is_user_facing(tmp_path: Path) -> None:
    """Wrap process startup OS errors in the buildx error type."""

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        raise OSError("permission denied")

    with pytest.raises(BuildxBuildError, match="could not be started"):
        build_image_with_buildx(
            image_tags=("image:tag",),
            context_dir=tmp_path,
            runner=runner,
            log=lambda message: None,
        )


def test_buildx_keyboard_interrupt_is_not_swallowed(tmp_path: Path) -> None:
    """Let user interrupts propagate instead of rewriting them as build errors."""

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        build_image_with_buildx(
            image_tags=("image:tag",),
            context_dir=tmp_path,
            runner=runner,
            log=lambda message: None,
        )
