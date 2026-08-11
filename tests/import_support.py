"""Package-module discovery for import-boundary tests."""

import pkgutil

import comfyui_docker_helper


def package_module_names() -> tuple[str, ...]:
    """Return every importable module exposed by the package."""
    package_paths = [str(path) for path in comfyui_docker_helper.__path__]
    discovered = sorted(
        module.name
        for module in pkgutil.walk_packages(
            package_paths,
            prefix=f"{comfyui_docker_helper.__name__}.",
        )
    )
    return (comfyui_docker_helper.__name__, *discovered)
