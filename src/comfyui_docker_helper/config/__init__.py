"""Public configuration, canonical lock, and BuildPlan interfaces."""

from importlib import import_module

_EXPORTS = {
    "AcceptedCanonicalLock": (
        "comfyui_docker_helper.config.canonical_resolver",
        "AcceptedCanonicalLock",
    ),
    "BAKED_RUNTIME_CONFIG_PATH": (
        "comfyui_docker_helper.config.runtime_config",
        "BAKED_RUNTIME_CONFIG_PATH",
    ),
    "BuildPlan": ("comfyui_docker_helper.config.build_plan", "BuildPlan"),
    "CanonicalLock": (
        "comfyui_docker_helper.config.canonical_lock",
        "CanonicalLock",
    ),
    "CanonicalLockError": (
        "comfyui_docker_helper.config.canonical_lock",
        "CanonicalLockError",
    ),
    "CanonicalResolutionError": (
        "comfyui_docker_helper.config.canonical_resolver",
        "CanonicalResolutionError",
    ),
    "ConfigurationResult": (
        "comfyui_docker_helper.config.service",
        "ConfigurationResult",
    ),
    "ConfigurationServiceError": (
        "comfyui_docker_helper.config.service",
        "ConfigurationServiceError",
    ),
    "Diagnostic": ("comfyui_docker_helper.config.diagnostics", "Diagnostic"),
    "DiagnosticSeverity": (
        "comfyui_docker_helper.config.diagnostics",
        "DiagnosticSeverity",
    ),
    "FinalConfig": ("comfyui_docker_helper.config.final_models", "FinalConfig"),
    "LockPolicy": (
        "comfyui_docker_helper.config.canonical_resolver",
        "LockPolicy",
    ),
    "MOUNTED_RUNTIME_CONFIG_PATH": (
        "comfyui_docker_helper.config.runtime_config",
        "MOUNTED_RUNTIME_CONFIG_PATH",
    ),
    "ManifestBinding": (
        "comfyui_docker_helper.config.build_plan",
        "ManifestBinding",
    ),
    "ReconcilePurpose": (
        "comfyui_docker_helper.config.canonical_resolver",
        "ReconcilePurpose",
    ),
    "RuntimeConfig": (
        "comfyui_docker_helper.config.runtime_models",
        "RuntimeConfig",
    ),
    "RuntimeConfigurationError": (
        "comfyui_docker_helper.config.runtime_config",
        "RuntimeConfigurationError",
    ),
    "RuntimeConfigurationResult": (
        "comfyui_docker_helper.config.runtime_config",
        "RuntimeConfigurationResult",
    ),
    "RuntimeSystemSshConfig": (
        "comfyui_docker_helper.config.runtime_models",
        "RuntimeSystemSshConfig",
    ),
    "build_plan_digest": (
        "comfyui_docker_helper.config.build_plan",
        "build_plan_digest",
    ),
    "construct_build_plan": (
        "comfyui_docker_helper.config.build_plan",
        "construct_build_plan",
    ),
    "dump_build_plan_json": (
        "comfyui_docker_helper.config.build_plan",
        "dump_build_plan_json",
    ),
    "dump_canonical_lock_toml": (
        "comfyui_docker_helper.config.canonical_lock",
        "dump_canonical_lock_toml",
    ),
    "load_canonical_lock": (
        "comfyui_docker_helper.config.canonical_lock",
        "load_canonical_lock",
    ),
    "load_runtime_config": (
        "comfyui_docker_helper.config.runtime_config",
        "load_runtime_config",
    ),
    "load_validate_config": (
        "comfyui_docker_helper.config.service",
        "load_validate_config",
    ),
    "load_validate_config_result": (
        "comfyui_docker_helper.config.service",
        "load_validate_config_result",
    ),
    "manifest_binding": (
        "comfyui_docker_helper.config.build_plan",
        "manifest_binding",
    ),
    "parse_build_plan_json": (
        "comfyui_docker_helper.config.build_plan",
        "parse_build_plan_json",
    ),
    "parse_canonical_lock_toml": (
        "comfyui_docker_helper.config.canonical_lock",
        "parse_canonical_lock_toml",
    ),
}


def __getattr__(name: str) -> object:
    """Load public authorities without creating package import cycles."""
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from error
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *_EXPORTS))


__all__ = [
    "BAKED_RUNTIME_CONFIG_PATH",
    "MOUNTED_RUNTIME_CONFIG_PATH",
    "AcceptedCanonicalLock",
    "BuildPlan",
    "CanonicalLock",
    "CanonicalLockError",
    "CanonicalResolutionError",
    "ConfigurationResult",
    "ConfigurationServiceError",
    "Diagnostic",
    "DiagnosticSeverity",
    "FinalConfig",
    "LockPolicy",
    "ManifestBinding",
    "ReconcilePurpose",
    "RuntimeConfig",
    "RuntimeConfigurationError",
    "RuntimeConfigurationResult",
    "RuntimeSystemSshConfig",
    "build_plan_digest",
    "construct_build_plan",
    "dump_build_plan_json",
    "dump_canonical_lock_toml",
    "load_canonical_lock",
    "load_runtime_config",
    "load_validate_config",
    "load_validate_config_result",
    "manifest_binding",
    "parse_build_plan_json",
    "parse_canonical_lock_toml",
]
