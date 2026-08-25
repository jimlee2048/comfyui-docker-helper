"""Architecture tests for the four production component boundaries."""

import ast
from pathlib import Path

from tests.project_paths import PROJECT_ROOT

PACKAGE_NAME = "comfyui_docker_helper"
SOURCE_ROOT = PROJECT_ROOT / "src" / "comfyui_docker_helper"
FORBIDDEN_COMPONENTS = {
    "cli_output": frozenset({"config", "host", "rendering", "container"}),
    "config": frozenset({"host", "rendering", "container"}),
    "rendering": frozenset({"host", "container"}),
    "host": frozenset({"container"}),
    "container": frozenset({"host", "rendering"}),
    "filesystem": frozenset({"host", "container", "rendering"}),
}
FORBIDDEN_CLI_OUTPUT_DEPENDENCIES = frozenset({"rich", "typer"})
CONFIG_AUTHORED_PREFIX = f"{PACKAGE_NAME}.config.authored"
CONFIG_RUNTIME_PREFIX = f"{PACKAGE_NAME}.config.runtime"
FORBIDDEN_CONFIG_VALIDATION_PREFIXES = (
    CONFIG_AUTHORED_PREFIX,
    CONFIG_RUNTIME_PREFIX,
)


def _imported_components(path: Path) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        for name in _imported_module_names(path, node):
            parts = name.split(".")
            if len(parts) > 1 and parts[0] == PACKAGE_NAME:
                imports.append((node.lineno, parts[1]))
    return tuple(imports)


def _imported_module_names(path: Path, node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return ()
    if node.level == 0:
        if node.module is None:
            return ()
        modules = [node.module]
        if node.module == PACKAGE_NAME or node.module.startswith(f"{PACKAGE_NAME}."):
            modules.extend(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if alias.name != "*"
            )
        return tuple(modules)

    package_parts = _source_package_parts(path)
    parent_count = node.level - 1
    if parent_count >= len(package_parts):
        return ()
    base = package_parts[: len(package_parts) - parent_count]
    module = tuple(node.module.split(".")) if node.module is not None else ()
    resolved = (*base, *module)
    if node.module is None:
        return tuple(".".join((*resolved, alias.name)) for alias in node.names)
    return (".".join(resolved),)


def _source_package_parts(path: Path) -> tuple[str, ...]:
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
    return (PACKAGE_NAME, *relative.parts[:-1])


def _is_in_module_namespace(name: str, prefix: str) -> bool:
    return (
        name == prefix or name.startswith(f"{prefix}.") or name.startswith(f"{prefix}_")
    )


def test_component_dependencies_follow_documented_direction() -> None:
    """Prevent inner components from importing outer orchestration layers."""
    violations = []
    for source, forbidden in FORBIDDEN_COMPONENTS.items():
        for path in sorted((SOURCE_ROOT / source).rglob("*.py")):
            for line, target in _imported_components(path):
                if target in forbidden:
                    relative = path.relative_to(SOURCE_ROOT)
                    violations.append(
                        f"{relative}:{line} imports forbidden component {target}"
                    )

    assert violations == []


def test_shared_cli_output_foundation_has_no_renderer_dependency() -> None:
    """Keep shared policy independent of concrete CLI presentation stacks."""
    violations = []
    for path in sorted((SOURCE_ROOT / "cli_output").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for name in _imported_module_names(path, node):
                root = name.split(".", maxsplit=1)[0]
                if root in FORBIDDEN_CLI_OUTPUT_DEPENDENCIES:
                    relative = path.relative_to(SOURCE_ROOT)
                    violations.append(f"{relative}:{node.lineno} imports {root}")

    assert violations == []


def test_config_validation_does_not_depend_on_authored_or_runtime_layers() -> None:
    """Keep shared validation upstream of authored and runtime orchestration."""
    violations = []
    for path in sorted((SOURCE_ROOT / "config" / "validation").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for name in _imported_module_names(path, node):
                if any(
                    _is_in_module_namespace(name, prefix)
                    for prefix in FORBIDDEN_CONFIG_VALIDATION_PREFIXES
                ):
                    relative = path.relative_to(SOURCE_ROOT)
                    violations.append(f"{relative}:{node.lineno} imports {name}")

    assert violations == []


def test_authored_and_runtime_config_layers_do_not_depend_on_each_other() -> None:
    """Keep authored composition and runtime admission as sibling layers."""
    config_root = SOURCE_ROOT / "config"
    layer_paths = {
        "authored": tuple((config_root / "authored").rglob("*.py")),
        "runtime": (
            *tuple((config_root / "runtime").rglob("*.py")),
            *tuple(config_root.glob("runtime*.py")),
        ),
    }
    forbidden_prefixes = {
        "authored": CONFIG_RUNTIME_PREFIX,
        "runtime": CONFIG_AUTHORED_PREFIX,
    }
    violations = []
    for layer, paths in layer_paths.items():
        forbidden = forbidden_prefixes[layer]
        for path in sorted(paths):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                for name in _imported_module_names(path, node):
                    if _is_in_module_namespace(name, forbidden):
                        relative = path.relative_to(SOURCE_ROOT)
                        violations.append(f"{relative}:{node.lineno} imports {name}")

    assert violations == []
