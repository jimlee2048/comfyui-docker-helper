"""Deterministic build-context materialization and safe replacement."""

import json
import shutil
import tempfile
from collections.abc import Iterable
from importlib import metadata, resources
from pathlib import Path, PurePosixPath

import tomli_w

from comfyui_docker_helper.config.lock import Lockfile, dump_lockfile_toml
from comfyui_docker_helper.config.models import Config
from comfyui_docker_helper.config.plan import (
    ArtifactKind,
    CustomNodesPlan,
    FilesPlan,
    GitCustomNodePlan,
    OutputArtifact,
    RegistryCustomNodePlan,
    RenderPlan,
)
from comfyui_docker_helper.rendering.dockerfile import render_dockerfile

_DEFERRED_MARKER_PATH = PurePosixPath(".cdh-rendered")
_MARKER_FILE = ".cdh-rendered"
_MARKER_PAYLOAD = {
    "tool": "comfyui-docker-helper",
    "kind": "build-context",
    "version": "0.1",
}
_DISTRIBUTION_NAME = "comfyui-docker-helper"
_PACKAGE_IMPORT_NAME = "comfyui_docker_helper"
_CONSOLE_SCRIPT_NAME = "cdh"
_CONSOLE_SCRIPT_VALUE = "comfyui_docker_helper.cli:app"
_BUILD_BACKEND_REQUIRES = ("uv_build>=0.11.23,<0.12",)
_BUILD_BACKEND = "uv_build"
_DIRECTORY_MODE = 0o755
_FILE_MODE = 0o644
_PACKAGE_CACHE_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
    }
)
_PACKAGE_CACHE_SUFFIXES = (".pyc", ".pyo")
_PACKAGE_METADATA_SUFFIXES = (".dist-info", ".egg-info")
type ConfigInput = str | Path | Iterable[str | Path]


class MaterializationError(RuntimeError):
    """A staging directory cannot be materialized to the render contract."""


class ContextWriteError(RuntimeError):
    """A build context cannot be written or replaced safely."""


def write_build_context(
    plan: RenderPlan,
    output_directory: str | Path,
    *,
    config: Config | None = None,
    lockfile: Lockfile | None = None,
    overwrite: bool = False,
    working_directory: str | Path | None = None,
    config_file: ConfigInput | None = None,
) -> None:
    """Materialize, mark, and safely replace a build-context directory.

    The destination is never mutated until a complete sibling staging directory
    has been materialized and marked. Existing directories are replaceable only
    when they have a valid marker and ``overwrite`` is true.
    """
    base = _resolve_existing_directory(
        Path.cwd() if working_directory is None else Path(working_directory),
        "working directory",
    )
    output = _resolve_output_path(Path(output_directory), base)
    config_files = _resolve_config_inputs(config_file, base)
    scripts_source = plan.custom_nodes.scripts_source_dir

    _validate_output_path(
        output,
        working_directory=base,
        config_files=config_files,
        scripts_source=scripts_source,
    )
    _validate_scripts_source_tree(plan)
    created_parent_directories = _ensure_output_parent(output)

    staging: Path | None = None
    try:
        overwrite_existing = _validate_destination_state(output, overwrite=overwrite)
        staging = _create_sibling_directory(output, "staging")
        materialize_build_context(plan, staging, config=config, lockfile=lockfile)
        _write_marker(staging)
        _replace_destination(staging, output, overwrite_existing=overwrite_existing)
    except BaseException:
        if staging is not None:
            _remove_path(staging)
        _remove_created_parent_directories(created_parent_directories)
        raise


def has_valid_context_marker(directory: str | Path) -> bool:
    """Return whether a directory contains a trusted cdh build-context marker."""
    marker = Path(directory) / _MARKER_FILE
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return all(payload.get(key) == value for key, value in _MARKER_PAYLOAD.items())


def serialize_custom_nodes_toml(plan: CustomNodesPlan) -> bytes:
    """Serialize normalized custom nodes as deterministic helper TOML bytes."""
    nodes: list[dict[str, object]] = []
    for node in plan.items:
        if isinstance(node, RegistryCustomNodePlan):
            item = _ordered_mapping(
                ("type", node.type),
                ("id", node.id),
            )
            if node.version is not None:
                item["version"] = node.version
        elif isinstance(node, GitCustomNodePlan):
            item = _ordered_mapping(
                ("type", node.type),
                ("url", node.url),
            )
            if node.ref is not None:
                item["ref"] = node.ref
            if node.target_dir is not None:
                item["target_dir"] = node.target_dir
        else:  # pragma: no cover - frozen plan union is exhaustive
            raise TypeError(f"unsupported custom-node plan: {type(node).__name__}")
        item["pre_install_scripts"] = list(node.pre_install_scripts)
        item["post_install_scripts"] = list(node.post_install_scripts)
        nodes.append(item)

    document = _ordered_mapping(
        (
            "comfyui",
            _ordered_mapping(("custom_nodes", nodes)),
        )
    )
    return tomli_w.dumps(document).encode("utf-8")


def serialize_files_toml(plan: FilesPlan) -> bytes:
    """Serialize normalized downloader settings and files as helper TOML bytes."""
    aria2 = plan.downloader.aria2
    httpx = plan.downloader.httpx
    downloader = _ordered_mapping(
        ("default", plan.downloader.default),
        (
            "aria2",
            _ordered_mapping(
                ("rpc_port", aria2.rpc_port),
                ("split", aria2.split),
                ("max_connection_per_server", aria2.max_connection_per_server),
                ("min_split_size", aria2.min_split_size),
                ("resume_download", aria2.resume_download),
            ),
        ),
        (
            "httpx",
            _ordered_mapping(
                ("timeout", httpx.timeout),
                ("retries", httpx.retries),
            ),
        ),
    )
    files = [
        _ordered_mapping(
            ("url", item.url),
            ("dir", item.directory),
            ("filename", item.filename),
            ("overwrite", item.overwrite),
            ("downloader", item.downloader),
        )
        for item in plan.items
    ]
    document = _ordered_mapping(
        ("downloader", downloader),
        ("files", files),
    )
    return tomli_w.dumps(document).encode("utf-8")


def serialize_config_toml(config: Config) -> bytes:
    """Serialize the merged effective root config as deterministic TOML bytes."""
    document = config.model_dump(mode="json", exclude_none=True)
    return tomli_w.dumps(document).encode("utf-8")


def materialize_build_context(
    plan: RenderPlan,
    staging_directory: str | Path,
    *,
    config: Config | None = None,
    lockfile: Lockfile | None = None,
) -> None:
    """Populate a caller-owned, existing, empty staging directory.

    This function deliberately does not write the marker or replace a destination.
    Callers that need atomic destination replacement should use
    ``write_build_context``. If materialization fails after the empty-directory
    precondition passes, only entries created inside the staging directory are
    removed; the staging directory itself remains.
    """
    destination = Path(staging_directory)
    _require_empty_staging_directory(destination)

    try:
        if (config is None) != (lockfile is None):
            raise MaterializationError(
                "root config and lock artifacts must be rendered together"
            )
        if config is not None and lockfile is not None:
            _write_bytes(destination / "config.toml", serialize_config_toml(config))
            _write_text(destination / "config.lock.toml", dump_lockfile_toml(lockfile))

        _write_text(destination / "Dockerfile", render_dockerfile(plan))
        _materialize_package_projection(destination / "packages" / "cdh")

        if plan.custom_nodes.items:
            _write_bytes(
                destination / "config" / "custom-nodes.toml",
                serialize_custom_nodes_toml(plan.custom_nodes),
            )
        if plan.files.items:
            _write_bytes(
                destination / "config" / "files.toml",
                serialize_files_toml(plan.files),
            )
        if plan.custom_nodes.has_hooks:
            scripts_source = plan.custom_nodes.scripts_source_dir
            if scripts_source is None:
                raise MaterializationError(
                    "render plan enables hooks without a scripts source directory"
                )
            _copy_plain_tree(scripts_source, destination / "scripts", "scripts")

        _reconcile_manifest(
            destination,
            _root_artifacts(config, lockfile) + plan.output_manifest.all,
        )
    except BaseException:
        _clear_staging_contents(destination)
        raise


def _ordered_mapping(*items: tuple[str, object]) -> dict[str, object]:
    """Construct a TOML mapping whose insertion order is explicit at the callsite."""
    return dict(items)


def _root_artifacts(
    config: Config | None,
    lockfile: Lockfile | None,
) -> tuple[OutputArtifact, ...]:
    if config is None and lockfile is None:
        return ()
    if config is None or lockfile is None:
        raise MaterializationError(
            "root config and lock artifacts must be rendered together"
        )
    return (
        OutputArtifact("config.toml", ArtifactKind.FILE),
        OutputArtifact("config.lock.toml", ArtifactKind.FILE),
    )


def _require_empty_staging_directory(destination: Path) -> None:
    if not destination.exists():
        raise MaterializationError("staging directory must already exist")
    if not destination.is_dir():
        raise MaterializationError("staging path must be a directory")
    if next(destination.iterdir(), None) is not None:
        raise MaterializationError("staging directory must be empty")


def _resolve_candidate_path(path: Path, base: Path) -> Path:
    candidate = path if path.is_absolute() else base / path
    return candidate.resolve(strict=False)


def _resolve_output_path(path: Path, base: Path) -> Path:
    candidate = path if path.is_absolute() else base / path
    if candidate.is_symlink():
        raise ContextWriteError("output directory must not be a symlink")
    return candidate.resolve(strict=False)


def _resolve_existing_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContextWriteError(f"{label} must exist") from exc
    if resolved.is_symlink() or not resolved.is_dir():
        raise ContextWriteError(f"{label} must be an existing real directory")
    return resolved


def _resolve_config_inputs(
    config_file: ConfigInput | None, base: Path
) -> tuple[Path, ...]:
    if config_file is None:
        return ()
    if isinstance(config_file, (str, Path)):
        return (_resolve_candidate_path(Path(config_file), base),)
    return tuple(_resolve_candidate_path(Path(path), base) for path in config_file)


def _validate_output_path(
    output: Path,
    *,
    working_directory: Path,
    config_files: tuple[Path, ...],
    scripts_source: Path | None,
) -> None:
    if output == Path(output.anchor):
        raise ContextWriteError("output directory must not be the filesystem root")
    if output.is_symlink():
        raise ContextWriteError("output directory must not be a symlink")

    protected_paths: list[tuple[str, Path]] = [("working directory", working_directory)]
    for config_file in config_files:
        protected_paths.append(
            ("configuration input", config_file.resolve(strict=False))
        )
    if scripts_source is not None:
        protected_paths.append(("scripts source", scripts_source.resolve(strict=False)))

    for label, protected in protected_paths:
        if _is_equal_or_ancestor(output, protected):
            raise ContextWriteError(
                f"output directory must not be equal to or an ancestor of {label}"
            )

    if scripts_source is not None:
        scripts = scripts_source.resolve(strict=False)
        if scripts in output.parents:
            raise ContextWriteError(
                "output directory must not be nested inside the scripts source"
            )


def _is_equal_or_ancestor(candidate: Path, target: Path) -> bool:
    return candidate == target or candidate in target.parents


def _validate_destination_state(output: Path, *, overwrite: bool) -> bool:
    if not output.exists():
        return False
    if output.is_symlink():
        raise ContextWriteError("output directory must not be a symlink")
    if not output.is_dir():
        raise ContextWriteError("output path already exists and is not a directory")
    if not has_valid_context_marker(output):
        raise ContextWriteError(
            "existing output directory is not a valid cdh build context"
        )
    if not overwrite:
        raise ContextWriteError(
            "existing output directory is already rendered; "
            "pass --overwrite to replace it"
        )
    return True


def _ensure_output_parent(output: Path) -> tuple[Path, ...]:
    parent = output.parent
    if parent.exists():
        if parent.is_symlink() or not parent.is_dir():
            raise ContextWriteError(
                "output parent path exists and is not a real directory"
            )
        return ()

    missing: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent

    if cursor.is_symlink() or not cursor.is_dir():
        raise ContextWriteError(
            "nearest existing output ancestor is not a real directory"
        )

    created: list[Path] = []
    try:
        for directory in reversed(missing):
            directory.mkdir()
            directory.chmod(_DIRECTORY_MODE)
            created.append(directory)
    except OSError as exc:
        _remove_created_parent_directories(tuple(reversed(created)))
        raise ContextWriteError("could not create output parent directories") from exc
    return tuple(reversed(created))


def _remove_created_parent_directories(directories: tuple[Path, ...]) -> None:
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            return


def _validate_scripts_source_tree(plan: RenderPlan) -> None:
    if not plan.custom_nodes.has_hooks:
        return
    source = plan.custom_nodes.scripts_source_dir
    if source is None:
        raise ContextWriteError(
            "render plan enables hooks without a scripts source directory"
        )
    if source.is_symlink() or not source.is_dir():
        raise ContextWriteError("scripts source must be a real directory")
    root = source.resolve(strict=True)
    _validate_plain_tree(source, "scripts source")
    for node in plan.custom_nodes.items:
        for hook in (*node.pre_install_scripts, *node.post_install_scripts):
            try:
                hook_path = (source / hook).resolve(strict=True)
            except OSError as exc:
                raise ContextWriteError(
                    f"referenced hook is missing from the scripts source: {hook}"
                ) from exc
            if not (hook_path == root or root in hook_path.parents):
                raise ContextWriteError(
                    f"referenced hook escapes the scripts source: {hook}"
                )


def _validate_plain_tree(source: Path, label: str) -> None:
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if child.is_symlink():
            raise ContextWriteError(f"{label} tree contains a symlink: {child.name}")
        if child.is_dir():
            _validate_plain_tree(child, label)
        elif not child.is_file():
            raise ContextWriteError(
                f"{label} tree contains a special file: {child.name}"
            )


def _create_sibling_directory(output: Path, kind: str) -> Path:
    prefix = f".{output.name}.cdh-{kind}-"
    try:
        created = Path(tempfile.mkdtemp(prefix=prefix, dir=output.parent))
    except OSError as exc:
        raise ContextWriteError(f"could not create sibling {kind} directory") from exc
    created.chmod(_DIRECTORY_MODE)
    return created


def _write_marker(directory: Path) -> None:
    marker = directory / _MARKER_FILE
    _write_bytes(
        marker,
        (
            json.dumps(_MARKER_PAYLOAD, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8"),
    )


def _replace_destination(
    staging: Path,
    output: Path,
    *,
    overwrite_existing: bool,
) -> None:
    if not overwrite_existing:
        if output.exists() or output.is_symlink():
            raise ContextWriteError("output path appeared during context rendering")
        staging.replace(output)
        return

    backup = _create_sibling_directory(output, "backup")
    _remove_path(backup)
    try:
        output.replace(backup)
        staging.replace(output)
    except BaseException:
        if not output.exists() and backup.exists():
            backup.replace(output)
        raise
    finally:
        _remove_path(backup)


def _materialize_package_projection(destination: Path) -> None:
    _ensure_directory(destination.parent)
    _ensure_directory(destination)
    _write_bytes(destination / "pyproject.toml", _generate_package_pyproject())
    package_destination = destination / "src" / _PACKAGE_IMPORT_NAME
    _ensure_directory(package_destination.parent)
    _ensure_directory(package_destination)

    try:
        package_resource = resources.files(_PACKAGE_IMPORT_NAME)
    except ModuleNotFoundError as exc:  # pragma: no cover - package imports itself
        raise MaterializationError(
            f"could not locate package resources for {_PACKAGE_IMPORT_NAME}"
        ) from exc

    with resources.as_file(package_resource) as package_source:
        if package_source.is_symlink() or not package_source.is_dir():
            raise MaterializationError(
                "package resource root must be a real directory: "
                f"{_PACKAGE_IMPORT_NAME}"
            )
        _copy_plain_tree(
            Path(package_source),
            package_destination,
            "package resource",
            skip_package_cache_entries=True,
        )


def _generate_package_pyproject() -> bytes:
    try:
        distribution = metadata.distribution(_DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError as exc:
        raise MaterializationError(
            f"could not locate installed distribution metadata for {_DISTRIBUTION_NAME}"
        ) from exc

    package_metadata = distribution.metadata
    project = _ordered_mapping(
        ("name", _metadata_required(package_metadata, "Name")),
        ("version", _metadata_required(package_metadata, "Version")),
        ("description", package_metadata.get("Summary", "")),
        ("requires-python", _metadata_required(package_metadata, "Requires-Python")),
        ("dependencies", list(distribution.requires or ())),
    )
    script = _find_console_script(distribution, _CONSOLE_SCRIPT_NAME)
    if script != _CONSOLE_SCRIPT_VALUE:
        raise MaterializationError(
            "installed distribution metadata must expose "
            f"{_CONSOLE_SCRIPT_NAME}={_CONSOLE_SCRIPT_VALUE}"
        )
    project["scripts"] = _ordered_mapping((_CONSOLE_SCRIPT_NAME, script))
    document = _ordered_mapping(
        ("project", project),
        (
            "build-system",
            _ordered_mapping(
                ("requires", _BUILD_BACKEND_REQUIRES),
                ("build-backend", _BUILD_BACKEND),
            ),
        ),
        (
            "tool",
            _ordered_mapping(
                (
                    "uv",
                    _ordered_mapping(
                        (
                            "build-backend",
                            _ordered_mapping(("module-name", _PACKAGE_IMPORT_NAME)),
                        )
                    ),
                )
            ),
        ),
    )
    return tomli_w.dumps(document).encode("utf-8")


def _metadata_required(package_metadata: metadata.PackageMetadata, key: str) -> str:
    value = package_metadata.get(key)
    if value is None or value == "":
        raise MaterializationError(
            f"installed distribution metadata is missing required field: {key}"
        )
    return value


def _find_console_script(distribution: metadata.Distribution, name: str) -> str:
    matches = distribution.entry_points.select(group="console_scripts", name=name)
    scripts = tuple(matches)
    if len(scripts) != 1:
        raise MaterializationError(
            f"installed distribution metadata must expose exactly one {name!r} "
            "console script"
        )
    return scripts[0].value


def _copy_plain_tree(
    source: Path,
    destination: Path,
    label: str,
    *,
    skip_package_cache_entries: bool = False,
) -> None:
    if source.is_symlink() or not source.is_dir():
        raise MaterializationError(f"{label} tree root must be a real directory")
    _ensure_directory(destination)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if skip_package_cache_entries and _should_skip_package_entry(child.name):
            continue
        target = destination / child.name
        if child.is_symlink():
            raise MaterializationError(
                f"{label} tree contains a symlink: {child.relative_to(source)}"
            )
        if child.is_dir():
            _ensure_directory(target)
            _copy_plain_tree(
                child,
                target,
                label,
                skip_package_cache_entries=skip_package_cache_entries,
            )
        elif child.is_file():
            _write_bytes(target, child.read_bytes())
            target.chmod(_FILE_MODE)
        else:
            raise MaterializationError(
                f"{label} tree contains a special file: {child.relative_to(source)}"
            )


def _should_skip_package_entry(name: str) -> bool:
    return (
        name in _PACKAGE_CACHE_DIRECTORIES
        or name.endswith(_PACKAGE_CACHE_SUFFIXES)
        or name.endswith(_PACKAGE_METADATA_SUFFIXES)
    )


def _write_text(path: Path, content: str) -> None:
    _ensure_directory(path.parent)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(_FILE_MODE)


def _write_bytes(path: Path, content: bytes) -> None:
    _ensure_directory(path.parent)
    path.write_bytes(content)
    path.chmod(_FILE_MODE)


def _ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(_DIRECTORY_MODE)


def _reconcile_manifest(
    destination: Path,
    artifacts: Iterable[OutputArtifact],
) -> None:
    expected = tuple(artifacts)
    marker = tuple(
        artifact
        for artifact in expected
        if PurePosixPath(artifact.path) == _DEFERRED_MARKER_PATH
    )
    if len(marker) != 1 or marker[0].kind is not ArtifactKind.FILE:
        raise MaterializationError(
            "output manifest must reserve .cdh-rendered as one file artifact"
        )

    materialized = tuple(
        artifact
        for artifact in expected
        if PurePosixPath(artifact.path) != _DEFERRED_MARKER_PATH
    )
    for artifact in materialized:
        path = destination.joinpath(*PurePosixPath(artifact.path).parts)
        if artifact.kind is ArtifactKind.FILE and not path.is_file():
            raise MaterializationError(
                f"materialized context is missing file artifact: {artifact.path}"
            )
        if artifact.kind is ArtifactKind.TREE and not path.is_dir():
            raise MaterializationError(
                f"materialized context is missing tree artifact: {artifact.path}"
            )

    unexpected = sorted(
        relative.as_posix()
        for path in destination.rglob("*")
        if not _is_manifest_path_allowed(
            relative := path.relative_to(destination), materialized
        )
    )
    if unexpected:
        raise MaterializationError(
            "materialized context contains unexpected artifacts: "
            + ", ".join(unexpected)
        )


def _is_manifest_path_allowed(
    relative: Path,
    artifacts: tuple[OutputArtifact, ...],
) -> bool:
    actual = PurePosixPath(relative.as_posix())
    for artifact in artifacts:
        expected = PurePosixPath(artifact.path)
        if actual == expected:
            return True
        if actual in expected.parents:
            return True
        if artifact.kind is ArtifactKind.TREE and expected in actual.parents:
            return True
    return False


def _clear_staging_contents(destination: Path) -> None:
    for child in destination.iterdir():
        _remove_path(child)


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
