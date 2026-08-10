"""Architecture tests for the four production component boundaries."""

import ast
from pathlib import Path

PACKAGE_NAME = "comfyui_docker_helper"
SOURCE_ROOT = Path(__file__).parents[2] / "src" / "comfyui_docker_helper"
FORBIDDEN_COMPONENTS = {
    "config": frozenset({"host", "rendering", "container"}),
    "rendering": frozenset({"host", "container"}),
    "host": frozenset({"container"}),
    "container": frozenset({"host", "rendering"}),
}


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
        if node.module == PACKAGE_NAME:
            return tuple(f"{PACKAGE_NAME}.{alias.name}" for alias in node.names)
        return (node.module,) if node.module is not None else ()

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
