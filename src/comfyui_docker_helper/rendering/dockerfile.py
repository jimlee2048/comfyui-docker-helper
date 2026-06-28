"""Safe deterministic rendering of the packaged Dockerfile template."""

import json
import re
import shlex
from collections.abc import Sequence

from jinja2 import Environment, PackageLoader, StrictUndefined

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


def render_dockerfile(plan: RenderPlan) -> str:
    """Render a complete Dockerfile using only an immutable normalized plan."""
    argument_names = tuple(argument.name for argument in plan.build_arguments)
    if argument_names != _BUILD_ARGUMENT_NAMES:
        raise ValueError(
            "render plan build arguments do not match the Dockerfile contract"
        )

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
