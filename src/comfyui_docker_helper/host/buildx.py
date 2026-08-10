"""Host-side Docker Buildx invocation service."""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Literal

from python_on_whales import DockerClient
from python_on_whales.exceptions import DockerException

from comfyui_docker_helper.errors import ApplicationError

type BuildxOutput = Literal["load", "push"]


class BuildxBuildError(ApplicationError):
    """A user-facing Docker Buildx invocation failure."""


BuildxLogger = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class BuildxOutputPlan:
    """Process-local image publication choices for one Buildx invocation."""

    tags: tuple[str, ...]
    output: BuildxOutput

    def __post_init__(self) -> None:
        if not self.tags:
            raise ValueError("a Buildx output plan requires at least one image tag")


@dataclass(frozen=True, slots=True)
class FileSecretBinding:
    """One host file bound to a stable BuildKit secret ID."""

    secret_id: str
    source: Path


def _render_file_secret(binding: FileSecretBinding) -> str:
    output = StringIO(newline="")
    csv.writer(output, lineterminator="").writerow(
        ("type=file", f"id={binding.secret_id}", f"src={binding.source}")
    )
    return output.getvalue()


def build_image_with_buildx(
    *,
    image_tags: Sequence[str],
    output: BuildxOutput = "load",
    context_dir: str | Path,
    platforms: Sequence[str] = ("linux/amd64",),
    cwd: str | Path | None = None,
    log: BuildxLogger = print,
    forward_default_ssh: bool = False,
    file_secret_bindings: Sequence[FileSecretBinding] = (),
    cache_from: str | None = None,
    cache_to: str | None = None,
) -> None:
    """Build one image through the public python-on-whales Buildx API."""
    base_directory = Path(cwd) if cwd is not None else Path.cwd()
    resolved_context = Path(context_dir)
    if not resolved_context.is_absolute():
        resolved_context = base_directory / resolved_context
    resolved_context = resolved_context.resolve()
    tags = tuple(image_tags)
    target_platforms = tuple(platforms)
    buildkit_inputs: dict[str, object] = {}
    if forward_default_ssh:
        buildkit_inputs["ssh"] = "default"
    if file_secret_bindings:
        buildkit_inputs["secrets"] = [
            _render_file_secret(binding) for binding in file_secret_bindings
        ]
    # The public python-on-whales API cannot express repeated opaque cache
    # specifications. Keep these values single and do not parse Buildx CSV.
    if cache_from is not None:
        buildkit_inputs["cache_from"] = cache_from
    if cache_to is not None:
        buildkit_inputs["cache_to"] = cache_to

    try:
        stream = DockerClient().buildx.build(
            resolved_context,
            tags=list(tags),
            load=output == "load",
            push=output == "push",
            platforms=list(target_platforms),
            progress="plain",
            stream_logs=True,
            **buildkit_inputs,
        )
        if stream is None:  # pragma: no cover - public API contract
            raise BuildxBuildError("Docker Buildx did not provide its live log stream")
        for message in stream:
            log(message.rstrip("\n"))
    except FileNotFoundError as error:
        raise BuildxBuildError(
            "Docker was not found; install Docker with Buildx and ensure it is on PATH"
        ) from error
    except KeyboardInterrupt:
        raise
    except OSError as error:
        raise BuildxBuildError("Docker Buildx could not be started") from error
    except DockerException as error:
        raise BuildxBuildError(
            f"Docker Buildx failed with exit code {error.return_code}"
        ) from error
