"""Cross-field and lexical path validation for public configuration."""

import re
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticPath
from comfyui_docker_helper.config.models import (
    Config,
    GitCustomNodeConfig,
    RegistryCustomNodeConfig,
)

_CUDA_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")
_SEMVER_PATTERN = re.compile(
    r"""
    (?:0|[1-9][0-9]*)\.
    (?:0|[1-9][0-9]*)\.
    (?:0|[1-9][0-9]*)
    (?:-
        (?:
            (?:0|[1-9][0-9]*)
            |[0-9]*[A-Za-z-][0-9A-Za-z-]*
        )
        (?:\.
            (?:
                (?:0|[1-9][0-9]*)
                |[0-9]*[A-Za-z-][0-9A-Za-z-]*
            )
        )*
    )?
    (?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?
    \Z
    """,
    re.VERBOSE,
)

_COMPUTE_PLATFORM_TYPES = frozenset({"cuda"})
_IMAGE_FLAVORS = frozenset({"base", "runtime", "devel", "cudnn-runtime", "cudnn-devel"})
_IMAGE_DISTROS = frozenset({"ubuntu22.04", "ubuntu24.04"})
_MANAGED_ENV_KEYS = frozenset(
    {
        "VIRTUAL_ENV",
        "PATH",
        "WORKSPACE",
        "COMFYUI_PATH",
        "DEBIAN_FRONTEND",
        "UV_LINK_MODE",
        "UV_PYTHON_CACHE_DIR",
    }
)
_DOWNLOADERS = frozenset({"aria2", "httpx"})
_HOOK_SUFFIXES = frozenset({".py", ".sh"})
_ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_GIT_TARGET_DIR_PATTERN = re.compile(r"[A-Za-z0-9._-]+\Z")
_DOCKERFILE_SOURCE_FORBIDDEN = frozenset({"\0", "\r", "\n"})


def normalize_comfyui_version(version: str) -> str:
    """Validate a ComfyUI release selector and remove its optional ``v`` prefix."""
    if version in {"latest", "nightly"}:
        return version

    normalized = version.removeprefix("v")
    if not _SEMVER_PATTERN.fullmatch(normalized):
        raise ValueError("must be latest, nightly, semver, or v-prefixed semver")
    return normalized


def normalize_comfy_cli_version(version: str) -> str:
    """Validate and canonicalize a comfy-cli public PEP 440 version."""
    if version == "latest":
        return version

    try:
        parsed = Version(version)
    except InvalidVersion as error:
        raise ValueError("must be latest or a PEP 440 public version") from error

    if parsed.local is not None:
        raise ValueError("must not contain a PEP 440 local-version label")
    return parsed.public


def infer_git_target_dir(url: str) -> str | None:
    """Return the default Git clone directory inferred from a repository URL."""
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""

    repo_name = PurePosixPath((path or url).rstrip("/")).name
    if repo_name.endswith(".git"):
        repo_name = repo_name.removesuffix(".git")
    return repo_name if repo_name and repo_name not in {".", ".."} else None


def is_safe_git_target_dir(target_dir: str) -> bool:
    """Return whether an explicit Git target directory is a safe basename."""
    return (
        target_dir not in {"", ".", ".."}
        and _GIT_TARGET_DIR_PATTERN.fullmatch(target_dir) is not None
    )


def resolve_git_target_dir(url: str, target_dir: str | None) -> str:
    """Resolve the effective Git target directory or raise ``ValueError``."""
    if target_dir is not None:
        if not is_safe_git_target_dir(target_dir):
            raise ValueError(
                "target_dir must match [A-Za-z0-9._-]+ and must not be . or .."
            )
        return target_dir

    inferred = infer_git_target_dir(url)
    if inferred is None:
        raise ValueError("cannot derive git target directory from URL")
    return inferred


def validate_config(
    config: Config,
    *,
    scripts_dir: str | Path | None = None,
) -> tuple[Diagnostic, ...]:
    """Collect all independent business and lexical errors in config order."""
    diagnostics: list[Diagnostic] = []

    _validate_compute_platform(config, diagnostics)
    _validate_system(config, diagnostics)
    _validate_pytorch(config, diagnostics)
    _validate_comfyui(config, scripts_dir, diagnostics)
    _validate_downloader(config, diagnostics)
    _validate_files(config, diagnostics)
    _validate_dockerfile_source_strings(config, diagnostics)

    return tuple(diagnostics)


def _validate_compute_platform(config: Config, diagnostics: list[Diagnostic]) -> None:
    compute_platform = config.compute_platform
    cuda = compute_platform.cuda

    _require_allowed(
        compute_platform.type,
        _COMPUTE_PLATFORM_TYPES,
        ("compute_platform", "type"),
        "compute_platform.unsupported_backend",
        "must be cuda",
        diagnostics,
    )
    if not _CUDA_VERSION_PATTERN.fullmatch(cuda.version):
        diagnostics.append(
            Diagnostic(
                path=("compute_platform", "cuda", "version"),
                code="compute_platform.invalid_cuda_version",
                message="must use major.minor or major.minor.patch numeric format",
            )
        )
    _require_allowed(
        cuda.image_flavor,
        _IMAGE_FLAVORS,
        ("compute_platform", "cuda", "image_flavor"),
        "compute_platform.unsupported_image_flavor",
        "must be base, runtime, devel, cudnn-runtime, or cudnn-devel",
        diagnostics,
    )
    _require_allowed(
        cuda.image_distro,
        _IMAGE_DISTROS,
        ("compute_platform", "cuda", "image_distro"),
        "compute_platform.unsupported_image_distro",
        "must be ubuntu22.04 or ubuntu24.04",
        diagnostics,
    )


def _validate_system(config: Config, diagnostics: list[Diagnostic]) -> None:
    system = config.system

    _require_absolute_container_path(
        system.workspace, ("system", "workspace"), diagnostics
    )
    if system.comfyui_path is not None:
        _require_absolute_container_path(
            system.comfyui_path, ("system", "comfyui_path"), diagnostics
        )
        if PurePosixPath(system.comfyui_path) == PurePosixPath(system.workspace):
            diagnostics.append(
                Diagnostic(
                    path=("system", "comfyui_path"),
                    code="system.comfyui_path_equals_workspace",
                    message=(
                        "must not equal system.workspace because comfy-cli install "
                        "requires owning the ComfyUI target directory"
                    ),
                )
            )

    for name in system.env:
        if name in _MANAGED_ENV_KEYS:
            diagnostics.append(
                Diagnostic(
                    path=("system", "env", name),
                    code="system.managed_env_override",
                    message=f"must not override managed environment variable {name}",
                )
            )
        if not _ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            diagnostics.append(
                Diagnostic(
                    path=("system", "env", name),
                    code="system.invalid_env_name",
                    message="name must match [A-Za-z_][A-Za-z0-9_]*",
                )
            )


def _validate_pytorch(config: Config, diagnostics: list[Diagnostic]) -> None:
    for index, package in enumerate(config.pytorch.extra_packages):
        if _is_torch_requirement(package):
            diagnostics.append(
                Diagnostic(
                    path=("pytorch", "extra_packages", index),
                    code="pytorch.duplicate_torch",
                    message="must not include the torch package",
                )
            )


def _validate_comfyui(
    config: Config,
    scripts_dir: str | Path | None,
    diagnostics: list[Diagnostic],
) -> None:
    comfyui = config.comfyui

    try:
        normalize_comfyui_version(comfyui.version)
    except ValueError as error:
        diagnostics.append(
            Diagnostic(
                path=("comfyui", "version"),
                code="comfyui.invalid_version",
                message=str(error),
            )
        )

    try:
        normalize_comfy_cli_version(comfyui.cli_version)
    except ValueError as error:
        diagnostics.append(
            Diagnostic(
                path=("comfyui", "cli_version"),
                code="comfyui.invalid_cli_version",
                message=str(error),
            )
        )

    if comfyui.custom_nodes and not comfyui.install_manager:
        diagnostics.append(
            Diagnostic(
                path=("comfyui", "install_manager"),
                code="comfyui.manager_required",
                message="must be true when custom nodes are configured",
            )
        )

    hook_paths = tuple(_iter_hooks(config))
    scripts_root = _validate_scripts_dir(hook_paths, scripts_dir, diagnostics)
    registry_ids: set[str] = set()
    git_urls: set[str] = set()
    git_target_dirs: dict[str, str] = {}

    for index, node in enumerate(comfyui.custom_nodes):
        node_path = ("comfyui", "custom_nodes", index)
        if isinstance(node, GitCustomNodeConfig):
            if node.url in git_urls:
                diagnostics.append(
                    Diagnostic(
                        path=(*node_path, "url"),
                        code="custom_node.duplicate_git_url",
                        message="git custom-node URLs must be unique",
                    )
                )
            git_urls.add(node.url)
            git_target_dir_path = (
                (*node_path, "target_dir")
                if node.target_dir is not None
                else (*node_path, "url")
            )
            try:
                git_target_dir = resolve_git_target_dir(node.url, node.target_dir)
            except ValueError as error:
                diagnostics.append(
                    Diagnostic(
                        path=git_target_dir_path,
                        code="custom_node.invalid_git_target_dir",
                        message=str(error),
                    )
                )
            else:
                existing_url = git_target_dirs.get(git_target_dir)
                if existing_url is not None and existing_url != node.url:
                    diagnostics.append(
                        Diagnostic(
                            path=git_target_dir_path,
                            code="custom_node.duplicate_git_target_dir",
                            message="git custom-node target directories must be unique",
                        )
                    )
                git_target_dirs.setdefault(git_target_dir, node.url)
        else:
            if node.id in registry_ids:
                diagnostics.append(
                    Diagnostic(
                        path=(*node_path, "id"),
                        code="custom_node.duplicate_registry_id",
                        message="registry custom-node IDs must be unique",
                    )
                )
            registry_ids.add(node.id)
        for hook, hook_path in _iter_node_hooks(index, node):
            _validate_hook(hook, hook_path, scripts_root, diagnostics)


def _validate_scripts_dir(
    hooks: tuple[tuple[str, DiagnosticPath], ...],
    scripts_dir: str | Path | None,
    diagnostics: list[Diagnostic],
) -> Path | None:
    if not hooks:
        return None
    if scripts_dir is None:
        diagnostics.append(
            Diagnostic(
                path=("scripts_dir",),
                code="hook.scripts_dir_required",
                message="is required when hooks are configured",
            )
        )
        return None

    scripts_root = Path(scripts_dir)
    if not scripts_root.is_dir():
        diagnostics.append(
            Diagnostic(
                path=("scripts_dir",),
                code="hook.scripts_dir_not_directory",
                message="must be an existing directory when hooks are configured",
            )
        )
        return None
    return scripts_root


def _iter_hooks(config: Config) -> Iterable[tuple[str, DiagnosticPath]]:
    for node_index, node in enumerate(config.comfyui.custom_nodes):
        yield from _iter_node_hooks(node_index, node)


def _iter_node_hooks(
    node_index: int,
    node: RegistryCustomNodeConfig | GitCustomNodeConfig,
) -> Iterable[tuple[str, DiagnosticPath]]:
    node_path: DiagnosticPath = ("comfyui", "custom_nodes", node_index)
    for field in ("pre_install_scripts", "post_install_scripts"):
        for hook_index, hook in enumerate(getattr(node, field)):
            yield hook, (*node_path, field, hook_index)


def _validate_hook(
    hook: str,
    path: DiagnosticPath,
    scripts_root: Path | None,
    diagnostics: list[Diagnostic],
) -> None:
    hook_path = PurePosixPath(hook)
    lexical_error = False

    if hook_path.is_absolute():
        diagnostics.append(
            Diagnostic(path, "hook.absolute_path", "must be relative to scripts-dir")
        )
        lexical_error = True
    if ".." in hook_path.parts:
        diagnostics.append(
            Diagnostic(path, "hook.parent_traversal", "must not contain '..'")
        )
        lexical_error = True
    if hook_path.suffix not in _HOOK_SUFFIXES:
        diagnostics.append(
            Diagnostic(path, "hook.unsupported_extension", "must end in .sh or .py")
        )
        lexical_error = True

    if scripts_root is not None and not lexical_error:
        source = scripts_root.joinpath(*hook_path.parts)
        if not source.is_file():
            diagnostics.append(
                Diagnostic(
                    path, "hook.source_not_file", "must reference an existing file"
                )
            )


def _validate_downloader(config: Config, diagnostics: list[Diagnostic]) -> None:
    _require_allowed(
        config.downloader.default,
        _DOWNLOADERS,
        ("downloader", "default"),
        "downloader.unsupported_default",
        "must be aria2 or httpx",
        diagnostics,
    )
    aria2 = config.downloader.aria2
    httpx = config.downloader.httpx
    _require_range(
        httpx.timeout,
        lambda value: value > 0,
        ("downloader", "httpx", "timeout"),
        "downloader.httpx_timeout_not_positive",
        "must be greater than 0",
        diagnostics,
    )
    _require_range(
        httpx.retries,
        lambda value: value >= 0,
        ("downloader", "httpx", "retries"),
        "downloader.httpx_retries_negative",
        "must be greater than or equal to 0",
        diagnostics,
    )
    _require_range(
        aria2.rpc_port,
        lambda value: 1 <= value <= 65535,
        ("downloader", "aria2", "rpc_port"),
        "downloader.aria2_rpc_port_out_of_range",
        "must be in range 1..65535",
        diagnostics,
    )
    _require_range(
        aria2.split,
        lambda value: value > 0,
        ("downloader", "aria2", "split"),
        "downloader.aria2_split_not_positive",
        "must be greater than 0",
        diagnostics,
    )
    _require_range(
        aria2.max_connection_per_server,
        lambda value: value > 0,
        ("downloader", "aria2", "max_connection_per_server"),
        "downloader.aria2_max_connection_per_server_not_positive",
        "must be greater than 0",
        diagnostics,
    )


def _validate_dockerfile_source_strings(
    config: Config,
    diagnostics: list[Diagnostic],
) -> None:
    """Reject line-breaking values before any Dockerfile rendering can occur."""
    values: list[tuple[str, DiagnosticPath]] = [
        (config.compute_platform.cuda.version, ("compute_platform", "cuda", "version")),
        (
            config.compute_platform.cuda.image_flavor,
            ("compute_platform", "cuda", "image_flavor"),
        ),
        (
            config.compute_platform.cuda.image_distro,
            ("compute_platform", "cuda", "image_distro"),
        ),
        (config.system.workspace, ("system", "workspace")),
        (config.python.version, ("python", "version")),
        (config.python.uv_version, ("python", "uv_version")),
        (config.pytorch.version, ("pytorch", "version")),
        (config.comfyui.cli_version, ("comfyui", "cli_version")),
        (config.comfyui.version, ("comfyui", "version")),
    ]
    if config.system.comfyui_path is not None:
        values.append((config.system.comfyui_path, ("system", "comfyui_path")))

    values.extend(
        (package, ("system", "extra_packages", index))
        for index, package in enumerate(config.system.extra_packages)
    )
    for name, value in config.system.env.items():
        values.append((name, ("system", "env", name)))
        values.append((value, ("system", "env", name)))
    values.extend(
        (package, ("python", "extra_packages", index))
        for index, package in enumerate(config.python.extra_packages)
    )
    values.extend(
        (package, ("pytorch", "extra_packages", index))
        for index, package in enumerate(config.pytorch.extra_packages)
    )
    values.extend(
        (argument, ("comfyui", "launch_args", index))
        for index, argument in enumerate(config.comfyui.launch_args)
    )

    for value, path in values:
        if any(character in value for character in _DOCKERFILE_SOURCE_FORBIDDEN):
            diagnostics.append(
                Diagnostic(
                    path=path,
                    code="dockerfile.invalid_source_character",
                    message="must not contain NUL, carriage return, or line feed",
                )
            )


def _validate_files(config: Config, diagnostics: list[Diagnostic]) -> None:
    for index, file in enumerate(config.files):
        file_path: DiagnosticPath = ("files", index)
        if not _is_http_url(file.url):
            diagnostics.append(
                Diagnostic(
                    (*file_path, "url"),
                    "file.invalid_url",
                    "must be an HTTP(S) URL with a host",
                )
            )

        directory = PurePosixPath(file.dir)
        if directory.is_absolute():
            diagnostics.append(
                Diagnostic(
                    (*file_path, "dir"),
                    "file.absolute_directory",
                    "must be relative to COMFYUI_PATH",
                )
            )
        if ".." in directory.parts:
            diagnostics.append(
                Diagnostic(
                    (*file_path, "dir"),
                    "file.directory_traversal",
                    "must not contain '..'",
                )
            )
        if not directory.parts or directory == PurePosixPath("."):
            diagnostics.append(
                Diagnostic(
                    (*file_path, "dir"),
                    "file.empty_directory",
                    "must be a non-empty relative directory",
                )
            )

        if not _is_safe_filename(file.filename):
            diagnostics.append(
                Diagnostic(
                    (*file_path, "filename"),
                    "file.invalid_filename",
                    "must be one nonempty filename component",
                )
            )

        if file.downloader is not None:
            _require_allowed(
                file.downloader,
                _DOWNLOADERS,
                (*file_path, "downloader"),
                "file.unsupported_downloader",
                "must be aria2 or httpx",
                diagnostics,
            )


def _require_allowed(
    value: str,
    allowed: frozenset[str],
    path: DiagnosticPath,
    code: str,
    message: str,
    diagnostics: list[Diagnostic],
) -> None:
    if value not in allowed:
        diagnostics.append(Diagnostic(path, code, message))


def _require_range(
    value: int | float,
    predicate: Callable[[int | float], bool],
    path: DiagnosticPath,
    code: str,
    message: str,
    diagnostics: list[Diagnostic],
) -> None:
    if not predicate(value):
        diagnostics.append(Diagnostic(path, code, message))


def _require_absolute_container_path(
    value: str,
    path: DiagnosticPath,
    diagnostics: list[Diagnostic],
) -> None:
    if not PurePosixPath(value).is_absolute():
        diagnostics.append(
            Diagnostic(
                path, "system.path_not_absolute", "must be an absolute POSIX path"
            )
        )


def _is_torch_requirement(package: str) -> bool:
    try:
        requirement = Requirement(package)
    except InvalidRequirement:
        return package.strip().casefold() == "torch"
    return canonicalize_name(requirement.name) == "torch"


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and bool(hostname)
        and "\\" not in parsed.netloc
        and not any(character.isspace() for character in parsed.netloc)
    )


def _is_safe_filename(filename: str) -> bool:
    return (
        bool(filename)
        and filename not in {".", ".."}
        and not any(separator in filename for separator in ("/", "\\"))
    )
