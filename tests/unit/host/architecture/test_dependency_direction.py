"""Architecture tests for current component and authority boundaries."""

import ast
from collections.abc import Mapping
from pathlib import Path

from tests.project_paths import PROJECT_ROOT

PACKAGE_NAME = "comfyui_docker_helper"
SOURCE_ROOT = PROJECT_ROOT / "src" / PACKAGE_NAME


def _package_namespace(value: str) -> str:
    return f"{PACKAGE_NAME}.{value}"


COMPONENT_DEPENDENCY_RULES = {
    _package_namespace("cli_output"): frozenset(
        {
            _package_namespace("config"),
            _package_namespace("host"),
            _package_namespace("rendering"),
            _package_namespace("container"),
        }
    ),
    _package_namespace("config"): frozenset(
        {
            _package_namespace("host"),
            _package_namespace("rendering"),
            _package_namespace("container"),
        }
    ),
    _package_namespace("rendering"): frozenset(
        {_package_namespace("host"), _package_namespace("container")}
    ),
    _package_namespace("host"): frozenset({_package_namespace("container")}),
    _package_namespace("container"): frozenset(
        {_package_namespace("host"), _package_namespace("rendering")}
    ),
    _package_namespace("filesystem"): frozenset(
        {
            _package_namespace("host"),
            _package_namespace("container"),
            _package_namespace("rendering"),
        }
    ),
}
CLI_OUTPUT_DEPENDENCY_RULES = {
    _package_namespace("cli_output"): frozenset({"rich", "typer"})
}
CONFIG_DEPENDENCY_RULES = {
    _package_namespace("config.authored"): frozenset(
        {_package_namespace("config.runtime")}
    ),
    _package_namespace("config.validation"): frozenset(
        {
            _package_namespace("config.authored"),
            _package_namespace("config.runtime"),
        }
    ),
    _package_namespace("config.credentials"): frozenset(
        {
            _package_namespace("config.authored"),
            _package_namespace("config.runtime"),
        }
    ),
    _package_namespace("config.planning"): frozenset(
        {
            _package_namespace("config.evidence"),
            _package_namespace("config.runtime"),
        }
    ),
    _package_namespace("config.runtime"): frozenset(
        {
            _package_namespace("config.authored"),
            _package_namespace("config.planning"),
        }
    ),
}
CONTAINER_DEPENDENCY_RULES = {
    _package_namespace("container.process"): frozenset(
        {
            _package_namespace("container.build"),
            _package_namespace("container.presentation"),
            _package_namespace("container.runtime"),
            _package_namespace("container.transfer"),
        }
    ),
    _package_namespace("container.transfer"): frozenset(
        {
            _package_namespace("container.build"),
            _package_namespace("container.presentation"),
            _package_namespace("container.runtime"),
        }
    ),
    _package_namespace("container.build"): frozenset(
        {
            _package_namespace("container.presentation"),
            _package_namespace("container.runtime"),
        }
    ),
    _package_namespace("container.presentation"): frozenset(
        {
            _package_namespace("container.build"),
            _package_namespace("container.transfer"),
        }
    ),
    _package_namespace("container.runtime"): frozenset(
        {
            _package_namespace("container.build"),
            _package_namespace("container.presentation"),
        }
    ),
}
CONTAINER_DEPENDENCY_EXCEPTIONS = {
    _package_namespace("container.presentation"): frozenset(
        {
            _package_namespace("container.build.events"),
            _package_namespace("container.transfer.cadence"),
            _package_namespace("container.transfer.events"),
        }
    )
}


def _source_package_parts(path: Path, source_root: Path) -> tuple[str, ...]:
    relative = path.relative_to(source_root).with_suffix("")
    return (PACKAGE_NAME, *relative.parts[:-1])


def _imported_module_names(
    path: Path,
    node: ast.AST,
    source_root: Path,
) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return ()

    if node.level == 0:
        if node.module is None:
            return ()
        base = node.module
    else:
        package_parts = _source_package_parts(path, source_root)
        parent_count = node.level - 1
        if parent_count >= len(package_parts):
            return ()
        parent = package_parts[: len(package_parts) - parent_count]
        module = tuple(node.module.split(".")) if node.module is not None else ()
        base = ".".join((*parent, *module))

    return tuple(
        base if alias.name == "*" else f"{base}.{alias.name}" for alias in node.names
    )


def _module_imports(
    path: Path,
    source_root: Path,
) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tuple(
        (node.lineno, name)
        for node in ast.walk(tree)
        for name in _imported_module_names(path, node, source_root)
    )


def _is_in_module_namespace(name: str, namespace: str) -> bool:
    return name == namespace or name.startswith(f"{namespace}.")


def _source_paths(source_root: Path, namespace: str) -> tuple[Path, ...]:
    prefix = f"{PACKAGE_NAME}."
    if not namespace.startswith(prefix):
        raise ValueError(f"source namespace must start with {prefix!r}")
    relative = namespace.removeprefix(prefix)
    namespace_root = source_root.joinpath(*relative.split("."))
    return tuple(sorted(namespace_root.rglob("*.py")))


def _dependency_violations(
    source_root: Path,
    rules: Mapping[str, frozenset[str]],
    exceptions: Mapping[str, frozenset[str]] | None = None,
) -> tuple[str, ...]:
    exceptions = {} if exceptions is None else exceptions
    violations: list[str] = []
    for source_namespace, forbidden_namespaces in rules.items():
        paths = _source_paths(source_root, source_namespace)
        if not paths:
            violations.append(
                f"{source_namespace}: source namespace has no Python modules"
            )
            continue
        allowed_namespaces = exceptions.get(source_namespace, frozenset())
        for path in paths:
            for line, imported_name in _module_imports(path, source_root):
                if any(
                    _is_in_module_namespace(imported_name, allowed)
                    for allowed in allowed_namespaces
                ):
                    continue
                forbidden = next(
                    (
                        namespace
                        for namespace in forbidden_namespaces
                        if _is_in_module_namespace(imported_name, namespace)
                    ),
                    None,
                )
                if forbidden is not None:
                    relative = path.relative_to(source_root).as_posix()
                    violations.append(
                        f"{relative}:{line} imports {imported_name} across "
                        f"forbidden {forbidden} boundary"
                    )
    return tuple(sorted(violations))


def test_component_dependencies_follow_documented_direction() -> None:
    """Prevent inner components from importing outer orchestration layers."""
    assert _dependency_violations(SOURCE_ROOT, COMPONENT_DEPENDENCY_RULES) == ()


def test_shared_cli_output_has_no_concrete_presenter_dependency() -> None:
    """Keep shared output policy independent of concrete presentation stacks."""
    assert _dependency_violations(SOURCE_ROOT, CLI_OUTPUT_DEPENDENCY_RULES) == ()


def test_config_layers_follow_documented_direction() -> None:
    """Keep authored, runtime, evidence, validation, and credential authority inward."""
    assert _dependency_violations(SOURCE_ROOT, CONFIG_DEPENDENCY_RULES) == ()


def test_container_layers_follow_documented_direction() -> None:
    """Keep process, transfer, build, presentation, and runtime owners one-way."""
    assert (
        _dependency_violations(
            SOURCE_ROOT,
            CONTAINER_DEPENDENCY_RULES,
            CONTAINER_DEPENDENCY_EXCEPTIONS,
        )
        == ()
    )


def test_dependency_checker_rejects_synthetic_violations(tmp_path: Path) -> None:
    """Prove the shared checker observes each stable rule category."""
    source_root = tmp_path / "src" / PACKAGE_NAME
    fixtures = {
        "cli_output/violation.py": "from rich import console\n",
        "config/authored/violation.py": "from .. import runtime\n",
        "config/runtime/violation.py": "from .. import authored\n",
        "config/validation/violation.py": "from .. import runtime\n",
        "container/transfer/violation.py": "from .. import runtime\n",
        "host/violation.py": f"from {PACKAGE_NAME} import container\n",
    }
    for relative, content in fixtures.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    rules = {
        _package_namespace("cli_output"): CLI_OUTPUT_DEPENDENCY_RULES[
            _package_namespace("cli_output")
        ],
        _package_namespace("config.authored"): CONFIG_DEPENDENCY_RULES[
            _package_namespace("config.authored")
        ],
        _package_namespace("config.runtime"): CONFIG_DEPENDENCY_RULES[
            _package_namespace("config.runtime")
        ],
        _package_namespace("config.validation"): CONFIG_DEPENDENCY_RULES[
            _package_namespace("config.validation")
        ],
        _package_namespace("container.transfer"): CONTAINER_DEPENDENCY_RULES[
            _package_namespace("container.transfer")
        ],
        _package_namespace("host"): COMPONENT_DEPENDENCY_RULES[
            _package_namespace("host")
        ],
    }

    assert _dependency_violations(source_root, rules) == (
        "cli_output/violation.py:1 imports rich.console across forbidden rich boundary",
        "config/authored/violation.py:1 imports "
        f"{PACKAGE_NAME}.config.runtime across forbidden "
        f"{PACKAGE_NAME}.config.runtime boundary",
        "config/runtime/violation.py:1 imports "
        f"{PACKAGE_NAME}.config.authored across forbidden "
        f"{PACKAGE_NAME}.config.authored boundary",
        "config/validation/violation.py:1 imports "
        f"{PACKAGE_NAME}.config.runtime across forbidden "
        f"{PACKAGE_NAME}.config.runtime boundary",
        "container/transfer/violation.py:1 imports "
        f"{PACKAGE_NAME}.container.runtime across forbidden "
        f"{PACKAGE_NAME}.container.runtime boundary",
        "host/violation.py:1 imports "
        f"{PACKAGE_NAME}.container across forbidden "
        f"{PACKAGE_NAME}.container boundary",
    )
