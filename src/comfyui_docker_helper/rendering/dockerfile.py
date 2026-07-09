"""Safe deterministic rendering of the packaged Dockerfile template."""

import json
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass

from jinja2 import Environment, PackageLoader, StrictUndefined

from comfyui_docker_helper.config.lock import Lockfile
from comfyui_docker_helper.config.plan import RenderPlan

_BUILD_ARGUMENT_NAMES = (
    "UV_IMAGE_TAG",
    "CUDA_IMAGE_TAG",
    "PYTHON_VERSION",
    "PYTORCH_VERSION",
    "PYTORCH_WHEEL_TAG",
    "COMFY_CLI_VERSION",
    "COMFYUI_VERSION",
    "UV_LINK_MODE",
    "UV_PYTHON_CACHE_DIR",
)
_GLOBAL_BUILD_ARGUMENT_NAMES = frozenset(_BUILD_ARGUMENT_NAMES[:2])
_DOCKERFILE_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DOCKERFILE_BARE_WORD_PATTERN = re.compile(r"[A-Za-z0-9_./:@%+,-]+\Z")
_FORBIDDEN_SOURCE_CHARACTERS = frozenset({"\0", "\r", "\n"})


@dataclass(frozen=True, slots=True)
class LockedComfyUIInstall:
    """Dockerfile-ready ComfyUI and comfy-cli selections from config.lock.toml."""

    cli_requirement: str
    install_arguments: tuple[str, ...]
    verify_stable_commit: bool
    commit: str


def serialize_dockerfile_identifier(value: str) -> str:
    """Return a validated Dockerfile identifier without quoting."""
    _ensure_source_line_safe(value)
    if not _DOCKERFILE_IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError("Dockerfile identifier must match [A-Za-z_][A-Za-z0-9_]*")
    return value


def serialize_dockerfile_word(value: str) -> str:
    """Serialize one literal Dockerfile instruction word."""
    _ensure_source_line_safe(value)
    if _DOCKERFILE_BARE_WORD_PATTERN.fullmatch(value):
        return value

    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def serialize_posix_shell_argument(value: str) -> str:
    """Serialize one literal argument for a POSIX shell-form RUN instruction."""
    _ensure_source_line_safe(value)
    return shlex.quote(value)


def serialize_json_array(values: Sequence[str]) -> str:
    """Serialize an exec-form Dockerfile JSON array."""
    for value in values:
        _ensure_source_line_safe(value)
    return json.dumps(list(values), ensure_ascii=False)


def render_dockerfile(plan: RenderPlan, *, lockfile: Lockfile | None = None) -> str:
    """Render a complete Dockerfile using immutable plan and lock selections."""
    argument_names = tuple(argument.name for argument in plan.build_arguments)
    if argument_names != _BUILD_ARGUMENT_NAMES:
        raise ValueError(
            "render plan build arguments do not match the Dockerfile contract"
        )
    locked_comfyui = _locked_comfyui_install(lockfile)

    build_arguments = {
        argument.name: argument.value for argument in plan.build_arguments
    }
    stage_arguments = tuple(
        argument
        for argument in plan.build_arguments
        if argument.name not in _GLOBAL_BUILD_ARGUMENT_NAMES
    )
    environment = _build_template_environment()
    template = environment.get_template("Dockerfile.j2")
    rendered = template.render(
        plan=plan,
        build_arguments=build_arguments,
        stage_arguments=stage_arguments,
        locked_comfyui=locked_comfyui,
        pytorch_extra_packages=plan.pytorch.requirements[1:],
    )
    return rendered if rendered.endswith("\n") else f"{rendered}\n"


def _build_template_environment() -> Environment:
    environment = Environment(
        loader=PackageLoader("comfyui_docker_helper", "templates"),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters.update(
        {
            "dockerfile_identifier": serialize_dockerfile_identifier,
            "dockerfile_word": serialize_dockerfile_word,
            "json_array": serialize_json_array,
            "shell_argument": serialize_posix_shell_argument,
        }
    )
    return environment


def _ensure_source_line_safe(value: str) -> None:
    if any(character in value for character in _FORBIDDEN_SOURCE_CHARACTERS):
        raise ValueError(
            "Dockerfile source values must not contain NUL, carriage return, "
            "or line feed"
        )


def _locked_comfyui_install(lockfile: Lockfile | None) -> LockedComfyUIInstall:
    if lockfile is None:
        raise ValueError("render_dockerfile requires config.lock.toml selections")

    comfyui = lockfile.comfyui
    _require_lock_value(comfyui.cli_version, ("comfyui", "cli_version"))
    _require_lock_value(comfyui.commit, ("comfyui", "commit"))
    if comfyui.version is None:
        install_arguments = ("--version", "nightly", "--commit", comfyui.commit)
        verify_stable_commit = False
    else:
        _require_lock_value(comfyui.version, ("comfyui", "version"))
        install_arguments = ("--version", comfyui.version)
        verify_stable_commit = True

    return LockedComfyUIInstall(
        cli_requirement=f"comfy-cli=={comfyui.cli_version}",
        install_arguments=install_arguments,
        verify_stable_commit=verify_stable_commit,
        commit=comfyui.commit,
    )


def _require_lock_value(value: str, path: tuple[str, str]) -> None:
    if not value:
        raise ValueError(f"config.lock.toml missing required [{path[0]}].{path[1]}")
    _ensure_source_line_safe(value)
