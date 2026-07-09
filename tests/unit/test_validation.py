"""Tests for business and lexical configuration validation."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config import (
    Config,
    Diagnostic,
    FileConfig,
    GitCustomNodeConfig,
    RegistryCustomNodeConfig,
    normalize_comfy_cli_version,
    normalize_comfyui_version,
    validate_config,
)
from comfyui_docker_helper.config.validation import (
    normalize_registry_version,
    resolve_git_target_dir,
)

VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "test@example"
)
TRUNCATED_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 truncated"


def make_config() -> Config:
    """Return a fresh minimal structurally valid configuration."""
    return Config.model_validate(
        {
            "compute_platform": {
                "type": "cuda",
                "cuda": {"version": "12.9.2"},
            },
            "pytorch": {"version": "2.10"},
            "comfyui": {"version": "latest"},
        }
    )


def locations_and_codes(
    diagnostics: tuple[Diagnostic, ...],
) -> list[tuple[tuple[str | int, ...], str]]:
    """Project diagnostics to their stable public identity."""
    return [(diagnostic.path, diagnostic.code) for diagnostic in diagnostics]


# Validation business basics: defaults, supported CUDA image shape, paths, and env.
def test_minimal_config_is_business_valid_without_host_or_network_checks() -> None:
    """Accept defaults without probing container paths, Docker, or the network."""
    config = make_config()
    config.system.workspace = "/path/that/does/not/exist/on/host"
    config.system.comfyui_path = "/another/nonexistent/container/path"

    assert validate_config(config) == ()


def test_system_ssh_defaults_are_public_config_defaults() -> None:
    config = make_config()

    assert config.system.ssh.enable is False
    assert config.system.ssh.port == 22
    assert config.system.ssh.password == ""
    assert config.system.ssh.pub_keys == []


def test_valid_system_ssh_public_key_is_accepted() -> None:
    config = make_config()
    config.system.ssh.pub_keys = ["", VALID_SSH_KEY]

    assert validate_config(config) == ()


def test_invalid_system_ssh_public_key_fails_without_leaking_password() -> None:
    config = make_config()
    config.system.ssh.password = "super-secret"
    config.system.ssh.pub_keys = ["not-a-key"]

    diagnostics = validate_config(config)
    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in diagnostics
    )

    assert locations_and_codes(diagnostics) == [
        (("system", "ssh", "pub_keys", 0), "ssh.invalid_public_key")
    ]
    assert "super-secret" not in payload


def test_truncated_base64_valid_system_ssh_public_key_fails() -> None:
    config = make_config()
    config.system.ssh.pub_keys = [TRUNCATED_SSH_KEY]

    assert locations_and_codes(validate_config(config)) == [
        (("system", "ssh", "pub_keys", 0), "ssh.invalid_public_key")
    ]


def test_diagnostics_and_result_are_immutable() -> None:
    """Expose immutable diagnostics in an immutable, stable result sequence."""
    config = make_config()
    config.compute_platform.type = "amd"

    diagnostics = validate_config(config)

    assert isinstance(diagnostics, tuple)
    with pytest.raises(FrozenInstanceError):
        diagnostics[0].code = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("version", ["12.8", "12.9.2", "13.0.2"])
def test_cuda_version_accepts_documented_numeric_formats(version: str) -> None:
    """Accept major.minor with an optional numeric patch."""
    config = make_config()
    config.compute_platform.cuda.version = version

    assert validate_config(config) == ()


@pytest.mark.parametrize(
    "version",
    ["12", "12.9.2.1", "v12.9", "12.x", "12.9-rc1", ""],
)
def test_cuda_version_rejects_other_formats(version: str) -> None:
    """Reject CUDA versions outside major.minor[.patch]."""
    config = make_config()
    config.compute_platform.cuda.version = version

    assert locations_and_codes(validate_config(config)) == [
        (
            ("compute_platform", "cuda", "version"),
            "compute_platform.invalid_cuda_version",
        )
    ]


def test_only_cuda_platform_is_supported() -> None:
    """Reject compute platform names outside the current CUDA-only contract."""
    config = make_config()
    config.compute_platform.type = "rocm"

    assert locations_and_codes(validate_config(config)) == [
        (("compute_platform", "type"), "compute_platform.unsupported_backend")
    ]


@pytest.mark.parametrize(
    "image_flavor",
    ["base", "runtime", "devel", "cudnn-runtime", "cudnn-devel"],
)
def test_approved_image_flavors_are_valid(image_flavor: str) -> None:
    """Accept every user-approved CUDA image flavor."""
    config = make_config()
    config.compute_platform.cuda.image_flavor = image_flavor

    assert validate_config(config) == ()


def test_unapproved_image_flavor_is_rejected() -> None:
    """Reject CUDA image tag flavors outside the approved finite set."""
    config = make_config()
    config.compute_platform.cuda.image_flavor = "cudnn"

    assert locations_and_codes(validate_config(config)) == [
        (
            ("compute_platform", "cuda", "image_flavor"),
            "compute_platform.unsupported_image_flavor",
        )
    ]


@pytest.mark.parametrize("image_distro", ["ubuntu22.04", "ubuntu24.04"])
def test_approved_image_distros_are_valid(image_distro: str) -> None:
    """Accept both user-approved Ubuntu CUDA image distros."""
    config = make_config()
    config.compute_platform.cuda.image_distro = image_distro

    assert validate_config(config) == ()


def test_unapproved_image_distro_is_rejected() -> None:
    """Reject distro tags outside the approved finite set."""
    config = make_config()
    config.compute_platform.cuda.image_distro = "ubuntu25.04"

    assert locations_and_codes(validate_config(config)) == [
        (
            ("compute_platform", "cuda", "image_distro"),
            "compute_platform.unsupported_image_distro",
        )
    ]


def test_container_paths_must_be_lexically_absolute() -> None:
    """Validate POSIX path shape without requiring host existence."""
    config = make_config()
    config.system.workspace = "workspace"
    config.system.comfyui_path = "ComfyUI"

    assert locations_and_codes(validate_config(config)) == [
        (("system", "workspace"), "system.path_not_absolute"),
        (("system", "comfyui_path"), "system.path_not_absolute"),
    ]


def test_comfyui_path_must_not_equal_workspace() -> None:
    """Avoid pre-creating the directory comfy-cli needs to checkout into."""
    config = make_config()
    config.system.workspace = "/workspace"
    config.system.comfyui_path = "/workspace"

    assert locations_and_codes(validate_config(config)) == [
        (("system", "comfyui_path"), "system.comfyui_path_equals_workspace")
    ]


@pytest.mark.parametrize(
    "managed_name",
    [
        "VIRTUAL_ENV",
        "PATH",
        "WORKSPACE",
        "COMFYUI_PATH",
        "DEBIAN_FRONTEND",
        "UV_LINK_MODE",
        "UV_PYTHON_CACHE_DIR",
    ],
)
def test_managed_environment_keys_cannot_be_overridden(managed_name: str) -> None:
    """Reject every environment variable managed by the rendered image."""
    config = make_config()
    config.system.env = {managed_name: "override"}

    assert locations_and_codes(validate_config(config)) == [
        (("system", "env", managed_name), "system.managed_env_override")
    ]


def test_environment_values_remain_strict_structural_strings() -> None:
    """Keep environment value typing in the public schema stage."""
    with pytest.raises(ValidationError) as raised:
        Config.model_validate(
            {
                "compute_platform": {
                    "type": "cuda",
                    "cuda": {"version": "12.9.2"},
                },
                "system": {"env": {"COUNT": 1}},
                "pytorch": {"version": "2.10"},
                "comfyui": {"version": "latest"},
            }
        )

    assert ("system", "env", "COUNT") in {
        error["loc"] for error in raised.value.errors()
    }


# Dockerfile-bound strings must stay safe to render into Dockerfile source lines.
@pytest.mark.parametrize("name", ["A", "_PRIVATE", "mixed_Case_123"])
def test_environment_names_accept_dockerfile_identifiers(name: str) -> None:
    """Accept environment names supported by the Dockerfile contract."""
    config = make_config()
    config.system.env = {name: "value"}

    assert validate_config(config) == ()


@pytest.mark.parametrize("name", ["", "1START", "HAS-DASH", "HAS.DOT", "HAS SPACE"])
def test_environment_names_reject_non_identifiers(name: str) -> None:
    """Reject names that cannot be emitted as safe ENV assignment keys."""
    config = make_config()
    config.system.env = {name: "value"}

    assert locations_and_codes(validate_config(config)) == [
        (("system", "env", name), "system.invalid_env_name")
    ]


@pytest.mark.parametrize("character", ["\0", "\r", "\n"])
@pytest.mark.parametrize(
    ("field", "path"),
    [
        ("version", ("compute_platform", "cuda", "version")),
        ("image_flavor", ("compute_platform", "cuda", "image_flavor")),
        ("image_distro", ("compute_platform", "cuda", "image_distro")),
        ("workspace", ("system", "workspace")),
        ("comfyui_path", ("system", "comfyui_path")),
        ("system_package", ("system", "extra_packages", 0)),
        ("environment_name", ("system", "env", "BAD")),
        ("environment_value", ("system", "env", "SAFE")),
        ("python_version", ("python", "version")),
        ("uv_version", ("python", "uv_version")),
        ("python_package", ("python", "extra_packages", 0)),
        ("pytorch_version", ("pytorch", "version")),
        ("pytorch_package", ("pytorch", "extra_packages", 0)),
        ("comfy_cli_version", ("comfyui", "cli_version")),
        ("comfyui_version", ("comfyui", "version")),
        ("listen", ("comfyui", "listen")),
        ("extra_argument", ("comfyui", "extra_args", 0)),
    ],
)
def test_dockerfile_bound_strings_reject_source_line_controls(
    field: str,
    path: tuple[str | int, ...],
    character: str,
) -> None:
    """Reject every configuration string emitted on a Dockerfile source line."""
    config = make_config()
    diagnostic_path = _set_dockerfile_bound_value(config, field, character)

    if field == "environment_name":
        assert diagnostic_path[:2] == ("system", "env")
        assert character in diagnostic_path[2]
    else:
        assert diagnostic_path == path
    assert (
        diagnostic_path,
        "dockerfile.invalid_source_character",
    ) in locations_and_codes(validate_config(config))


def _set_dockerfile_bound_value(
    config: Config,
    field: str,
    character: str,
) -> tuple[str | int, ...]:
    value = f"safe{character}value"
    if field == "version":
        config.compute_platform.cuda.version = value
        return ("compute_platform", "cuda", "version")
    if field == "image_flavor":
        config.compute_platform.cuda.image_flavor = value
        return ("compute_platform", "cuda", "image_flavor")
    if field == "image_distro":
        config.compute_platform.cuda.image_distro = value
        return ("compute_platform", "cuda", "image_distro")
    if field == "workspace":
        config.system.workspace = f"/{value}"
        return ("system", "workspace")
    if field == "comfyui_path":
        config.system.comfyui_path = f"/{value}"
        return ("system", "comfyui_path")
    if field == "system_package":
        config.system.extra_packages = [value]
        return ("system", "extra_packages", 0)
    if field == "environment_name":
        name = f"BAD{character}NAME"
        config.system.env = {name: "value"}
        return ("system", "env", name)
    if field == "environment_value":
        config.system.env = {"SAFE": value}
        return ("system", "env", "SAFE")
    if field == "python_version":
        config.python.version = value
        return ("python", "version")
    if field == "uv_version":
        config.python.uv_version = value
        return ("python", "uv_version")
    if field == "python_package":
        config.python.extra_packages = [value]
        return ("python", "extra_packages", 0)
    if field == "pytorch_version":
        config.pytorch.version = value
        return ("pytorch", "version")
    if field == "pytorch_package":
        config.pytorch.extra_packages = [value]
        return ("pytorch", "extra_packages", 0)
    if field == "comfy_cli_version":
        config.comfyui.cli_version = value
        return ("comfyui", "cli_version")
    if field == "comfyui_version":
        config.comfyui.version = value
        return ("comfyui", "version")
    if field == "listen":
        config.comfyui.listen = value
        return ("comfyui", "listen")
    if field == "extra_argument":
        config.comfyui.extra_args = [value]
        return ("comfyui", "extra_args", 0)
    raise AssertionError(f"unknown test field: {field}")


@pytest.mark.parametrize(
    "argument",
    [
        "--listen",
        "--listen=127.0.0.1",
        "--port",
        "--port=8190",
        "--auto-launch",
        "--auto-launch=true",
        "--disable-auto-launch",
        "--disable-auto-launch=true",
    ],
)
def test_comfyui_extra_args_reject_cdh_controlled_startup_flags(
    argument: str,
) -> None:
    """Keep cdh-owned startup flags out of passthrough ComfyUI arguments."""
    config = make_config()
    config.comfyui.extra_args = ["--cpu", argument]

    diagnostics = validate_config(config)

    assert locations_and_codes(diagnostics) == [
        (("comfyui", "extra_args", 1), "comfyui.controlled_extra_arg")
    ]
    assert argument.split("=", maxsplit=1)[0] in diagnostics[0].message


@pytest.mark.parametrize(
    "torch_requirement",
    ["torch", "Torch==2.10", "torch[opt]>=2", "torch @ https://example.com/torch.whl"],
)
def test_pytorch_extra_packages_cannot_duplicate_torch(
    torch_requirement: str,
) -> None:
    """Reject PEP 508 requirements whose normalized project name is torch."""
    config = make_config()
    config.pytorch.extra_packages = ["torchvision", torch_requirement]

    assert locations_and_codes(validate_config(config)) == [
        (("pytorch", "extra_packages", 1), "pytorch.duplicate_torch")
    ]


def test_required_pytorch_version_remains_structural() -> None:
    """Report an omitted required PyTorch version in the public schema stage."""
    with pytest.raises(ValidationError) as raised:
        Config.model_validate(
            {
                "compute_platform": {
                    "type": "cuda",
                    "cuda": {"version": "12.9.2"},
                },
                "pytorch": {},
                "comfyui": {"version": "latest"},
            }
        )

    assert (("pytorch", "version"), "missing") in {
        (error["loc"], error["type"]) for error in raised.value.errors()
    }


# Selector validation keeps accepted version ranges precise and normalized.
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("python_index", "http://mirror.example.com/simple"),
        ("python_index", "https://mirror.example.com/simple"),
        ("pytorch_index", "http://mirror.example.com/whl"),
        ("pytorch_index", "https://mirror.example.com/whl"),
    ],
)
def test_package_index_urls_accept_http_and_https(field: str, value: str) -> None:
    """Accept package index URLs with an HTTP(S) scheme and host."""
    config = make_config()
    if field == "python_index":
        config.python.index_url = value
    else:
        config.pytorch.index_base_url = value

    assert validate_config(config) == ()


@pytest.mark.parametrize(
    ("field", "value", "path", "code"),
    [
        (
            "python_index",
            "file:///tmp/simple",
            ("python", "index_url"),
            "python.invalid_index_url",
        ),
        (
            "python_index",
            "https://user:token@example.com/simple",
            ("python", "index_url"),
            "python.invalid_index_url",
        ),
        (
            "pytorch_index",
            "ftp://example.com/whl",
            ("pytorch", "index_base_url"),
            "pytorch.invalid_index_base_url",
        ),
        (
            "pytorch_index",
            "https://token@example.com/whl",
            ("pytorch", "index_base_url"),
            "pytorch.invalid_index_base_url",
        ),
    ],
)
def test_package_index_urls_reject_non_http_and_userinfo(
    field: str,
    value: str,
    path: tuple[str | int, ...],
    code: str,
) -> None:
    """Reject non-HTTP(S) indexes and TOML-embedded index credentials."""
    config = make_config()
    if field == "python_index":
        config.python.index_url = value
    else:
        config.pytorch.index_base_url = value

    assert locations_and_codes(validate_config(config)) == [(path, code)]


@pytest.mark.parametrize(
    ("version", "normalized"),
    [
        ("latest", "latest"),
        ("nightly", "nightly"),
        ("1.2.3", "1.2.3"),
        ("v1.2.3", "1.2.3"),
        ("1.2.3-rc.1+build.5", "1.2.3-rc.1+build.5"),
        (">=1.0,<2", "<2,>=1.0"),
        ("<2", "<2"),
        ("!=1.2.3", "!=1.2.3"),
    ],
)
def test_comfyui_versions_validate_and_normalize(version: str, normalized: str) -> None:
    """Keep selectors and semver while stripping only a leading v."""
    assert normalize_comfyui_version(version) == normalized

    config = make_config()
    config.comfyui.version = version
    assert validate_config(config) == ()


@pytest.mark.parametrize(
    "version",
    [
        "1.2",
        "v1.2",
        "V1.2.3",
        "01.2.3",
        "commit:abc",
        "https://example.com",
        "~=1.2",
        "==1.*",
        "^1.2.3",
        "~1.2.3",
        ">=1 || <2",
        "===1.2.3",
        ">=1,,<2",
        "==1.0+local",
        "!=1.0+local",
    ],
)
def test_invalid_comfyui_versions_are_rejected(version: str) -> None:
    """Reject non-semver and unsupported ComfyUI install modes."""
    config = make_config()
    config.comfyui.version = version

    assert locations_and_codes(validate_config(config)) == [
        (("comfyui", "version"), "comfyui.invalid_version")
    ]
    with pytest.raises(ValueError):
        normalize_comfyui_version(version)


@pytest.mark.parametrize(
    ("version", "normalized"),
    [
        ("latest", "latest"),
        ("1.0", "1.0"),
        ("v2.0RC1", "2.0rc1"),
        ("1!2.0.post1", "1!2.0.post1"),
        (">=1.0,<2", "<2,>=1.0"),
        ("<2", "<2"),
        ("==1.2.3", "==1.2.3"),
    ],
)
def test_comfy_cli_versions_use_packaging_normalization(
    version: str, normalized: str
) -> None:
    """Canonicalize accepted comfy-cli versions with packaging.Version."""
    assert normalize_comfy_cli_version(version) == normalized

    config = make_config()
    config.comfyui.cli_version = version
    assert validate_config(config) == ()


@pytest.mark.parametrize(
    "version",
    [
        "1.0+local",
        "https://example.com/package.whl",
        "git+https://example.com/repo.git",
        "nightly",
        "~=1.2",
        "==1.*",
        "^1.2.3",
        "~1.2.3",
        ">=1 || <2",
        "===1.2.3",
        ">=1,,<2",
    ],
)
def test_invalid_comfy_cli_versions_are_rejected(version: str) -> None:
    """Reject unsupported constraints, local labels, URLs, VCS, and labels."""
    config = make_config()
    config.comfyui.cli_version = version

    assert locations_and_codes(validate_config(config)) == [
        (("comfyui", "cli_version"), "comfyui.invalid_cli_version")
    ]
    with pytest.raises(ValueError):
        normalize_comfy_cli_version(version)


@pytest.mark.parametrize(
    ("version", "normalized"),
    [
        ("latest", "latest"),
        ("1.2.3", "1.2.3"),
        ("v1.2.3", "1.2.3"),
        (">=1.0,<2", "<2,>=1.0"),
        ("<2", "<2"),
    ],
)
def test_registry_versions_validate_and_normalize(
    version: str,
    normalized: str,
) -> None:
    """Accept latest, exact semver, and supported constraints for registry nodes."""
    assert normalize_registry_version(version) == normalized

    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "node", "version": version}
        )
    ]
    assert validate_config(config) == ()


@pytest.mark.parametrize(
    "version",
    ["1.2", "channel:beta", "~=1.2", "==1.*", "^1.2.3", ">=1 || <2", "===1.2.3"],
)
def test_invalid_registry_versions_are_rejected(version: str) -> None:
    """Reject arbitrary registry selectors and unsupported constraint syntax."""
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "node", "version": version}
        )
    ]

    assert locations_and_codes(validate_config(config)) == [
        (
            ("comfyui", "custom_nodes", 0, "version"),
            "custom_node.invalid_registry_version",
        )
    ]
    with pytest.raises(ValueError):
        normalize_registry_version(version)


@pytest.mark.parametrize(
    ("field", "path"),
    [
        ("python_version", ("python", "version")),
        ("uv_version", ("python", "uv_version")),
        ("pytorch_version", ("pytorch", "version")),
        ("system_package", ("system", "extra_packages", 0)),
        ("file_name", ("files", 0, "filename")),
    ],
)
def test_constraints_are_rejected_on_unsupported_fields(
    field: str,
    path: tuple[str | int, ...],
) -> None:
    """Keep constraints limited to fields with later lock-domain resolution."""
    config = make_config()
    if field == "python_version":
        config.python.version = "3.12,<3.13"
    elif field == "uv_version":
        config.python.uv_version = "latest,<1"
    elif field == "pytorch_version":
        config.pytorch.version = "2.7.1,<3"
    elif field == "system_package":
        config.system.extra_packages = ["curl>=8"]
    elif field == "file_name":
        config.files = [
            FileConfig.model_validate(
                {
                    "url": "https://example.com/model.bin",
                    "dir": "models",
                    "filename": "model>=1.bin",
                }
            )
        ]
    else:
        raise AssertionError(f"unknown test field: {field}")

    assert locations_and_codes(validate_config(config)) == [
        (path, "version_constraint.unsupported_field")
    ]


def test_cuda_constraints_are_rejected_without_broadening_cuda_versions() -> None:
    """Keep CUDA on its numeric image-tag contract and reject constraints."""
    config = make_config()
    config.compute_platform.cuda.version = ">=12"

    assert locations_and_codes(validate_config(config)) == [
        (
            ("compute_platform", "cuda", "version"),
            "compute_platform.invalid_cuda_version",
        ),
        (
            ("compute_platform", "cuda", "version"),
            "version_constraint.unsupported_field",
        ),
    ]


def test_custom_nodes_require_manager() -> None:
    """Require Manager whenever at least one custom node is configured."""
    config = make_config()
    config.comfyui.install_manager = False
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate({"type": "registry", "id": "node"})
    ]

    assert locations_and_codes(validate_config(config)) == [
        (("comfyui", "install_manager"), "comfyui.manager_required")
    ]


def test_registry_ids_must_be_unique_in_input_order() -> None:
    """Report every registry duplicate after its first declaration."""
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "same", "version": "1.0.0"}
        ),
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "same", "version": "2.0.0"}
        ),
        RegistryCustomNodeConfig.model_validate({"type": "registry", "id": "same"}),
    ]

    assert locations_and_codes(validate_config(config)) == [
        (
            ("comfyui", "custom_nodes", 1, "id"),
            "custom_node.duplicate_registry_id",
        ),
        (
            ("comfyui", "custom_nodes", 2, "id"),
            "custom_node.duplicate_registry_id",
        ),
    ]


def test_git_urls_must_be_unique_even_with_different_refs() -> None:
    """Use the Git URL, not URL/ref target, as the uniqueness key."""
    config = make_config()
    config.comfyui.custom_nodes = [
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/node.git", "ref": "one"}
        ),
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/node.git", "ref": "two"}
        ),
    ]

    assert locations_and_codes(validate_config(config)) == [
        (
            ("comfyui", "custom_nodes", 1, "url"),
            "custom_node.duplicate_git_url",
        )
    ]


def test_git_target_dir_accepts_safe_single_directory_name() -> None:
    """Allow explicit Git clone directory names that stay within custom_nodes."""
    config = make_config()
    config.comfyui.custom_nodes = [
        GitCustomNodeConfig.model_validate(
            {
                "type": "git",
                "url": "https://example.com/upstream.git",
                "target_dir": "ComfyUI_Example-1.2",
            }
        )
    ]

    assert validate_config(config) == ()


@pytest.mark.parametrize(
    ("url", "target_dir", "expected"),
    [
        ("https://example.com/repo.git", None, "repo"),
        ("https://example.com/repo", None, "repo"),
        ("https://example.com/nested/repo/", None, "repo"),
        ("git@example.com:org/repo.git", None, "repo"),
        ("https://example.com/upstream.git", "custom-name", "custom-name"),
    ],
)
def test_git_target_dir_resolution_preserves_url_basename_contract(
    url: str,
    target_dir: str | None,
    expected: str,
) -> None:
    """Keep Git URL basename inference stable for explicit and inferred targets."""
    assert resolve_git_target_dir(url, target_dir) == expected


@pytest.mark.parametrize(
    "target_dir",
    [
        "",
        ".",
        "..",
        "/absolute",
        "nested/node",
        "nested\\node",
        "../node",
        "node/..",
        "node name",
        "node:name",
        "node@main",
    ],
)
def test_git_target_dir_must_be_safe_single_directory_name(
    target_dir: str,
) -> None:
    """Reject explicit Git target directories that are paths or unsafe names."""
    config = make_config()
    config.comfyui.custom_nodes = [
        GitCustomNodeConfig.model_validate(
            {
                "type": "git",
                "url": "https://example.com/node.git",
                "target_dir": target_dir,
            }
        )
    ]

    assert locations_and_codes(validate_config(config)) == [
        (
            ("comfyui", "custom_nodes", 0, "target_dir"),
            "custom_node.invalid_git_target_dir",
        )
    ]


def test_git_effective_target_dirs_must_be_unique_for_different_urls() -> None:
    """Reject clone directory collisions before Docker build execution."""
    config = make_config()
    config.comfyui.custom_nodes = [
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/shared.git"}
        ),
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://mirror.example.com/shared"}
        ),
        GitCustomNodeConfig.model_validate(
            {
                "type": "git",
                "url": "https://example.org/other.git",
                "target_dir": "shared",
            }
        ),
    ]

    assert locations_and_codes(validate_config(config)) == [
        (
            ("comfyui", "custom_nodes", 1, "url"),
            "custom_node.duplicate_git_target_dir",
        ),
        (
            ("comfyui", "custom_nodes", 2, "target_dir"),
            "custom_node.duplicate_git_target_dir",
        ),
    ]


# Hook validation covers conditional scripts-dir use and hook path safety.
def test_no_hooks_do_not_require_or_validate_scripts_dir(tmp_path: Path) -> None:
    """Ignore scripts-dir entirely when no hook is referenced."""
    config = make_config()

    assert validate_config(config) == ()
    assert validate_config(config, scripts_dir=tmp_path / "missing") == ()


def test_hooks_require_scripts_dir() -> None:
    """Require a scripts source only when a hook is referenced."""
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "node",
                "pre_install_scripts": ["prepare.sh"],
            }
        )
    ]

    assert locations_and_codes(validate_config(config)) == [
        (("scripts_dir",), "hook.scripts_dir_required")
    ]


def test_hooks_require_scripts_dir_to_be_an_existing_directory(
    tmp_path: Path,
) -> None:
    """Reject missing paths and regular files as scripts directories."""
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "node",
                "pre_install_scripts": ["prepare.sh"],
            }
        )
    ]
    regular_file = tmp_path / "regular-file"
    regular_file.write_text("not a directory", encoding="utf-8")

    for scripts_dir in (tmp_path / "missing", regular_file):
        assert locations_and_codes(
            validate_config(config, scripts_dir=scripts_dir)
        ) == [(("scripts_dir",), "hook.scripts_dir_not_directory")]


def test_valid_shell_and_python_hooks_exist_under_scripts_dir(
    tmp_path: Path,
) -> None:
    """Accept ordered nested hook paths that reference regular source files."""
    scripts_dir = tmp_path / "scripts"
    nested_dir = scripts_dir / "nested"
    nested_dir.mkdir(parents=True)
    (scripts_dir / "prepare.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (nested_dir / "finish.py").write_text("pass\n", encoding="utf-8")
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "node",
                "pre_install_scripts": ["prepare.sh"],
                "post_install_scripts": ["nested/finish.py"],
            }
        )
    ]

    assert validate_config(config, scripts_dir=scripts_dir) == ()


def test_hook_paths_reject_absolute_traversal_and_unsupported_extensions(
    tmp_path: Path,
) -> None:
    """Collect lexical hook failures in node/list declaration order."""
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "node",
                "pre_install_scripts": ["/absolute.sh", "../escape.py"],
                "post_install_scripts": ["notes.txt"],
            }
        )
    ]

    assert locations_and_codes(validate_config(config, scripts_dir=tmp_path)) == [
        (
            ("comfyui", "custom_nodes", 0, "pre_install_scripts", 0),
            "hook.absolute_path",
        ),
        (
            ("comfyui", "custom_nodes", 0, "pre_install_scripts", 1),
            "hook.parent_traversal",
        ),
        (
            ("comfyui", "custom_nodes", 0, "post_install_scripts", 0),
            "hook.unsupported_extension",
        ),
    ]


def test_hook_source_must_be_a_file(tmp_path: Path) -> None:
    """Reject missing hook sources and directories with supported suffixes."""
    (tmp_path / "directory.py").mkdir()
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "node",
                "pre_install_scripts": ["missing.sh", "directory.py"],
            }
        )
    ]

    assert locations_and_codes(validate_config(config, scripts_dir=tmp_path)) == [
        (
            ("comfyui", "custom_nodes", 0, "pre_install_scripts", 0),
            "hook.source_not_file",
        ),
        (
            ("comfyui", "custom_nodes", 0, "pre_install_scripts", 1),
            "hook.source_not_file",
        ),
    ]


def test_node_diagnostics_follow_node_and_field_order(tmp_path: Path) -> None:
    """Keep a node's hook errors before errors from later node declarations."""
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "same",
                "pre_install_scripts": ["missing.sh"],
            }
        ),
        RegistryCustomNodeConfig.model_validate({"type": "registry", "id": "same"}),
    ]

    assert locations_and_codes(validate_config(config, scripts_dir=tmp_path)) == [
        (
            ("comfyui", "custom_nodes", 0, "pre_install_scripts", 0),
            "hook.source_not_file",
        ),
        (
            ("comfyui", "custom_nodes", 1, "id"),
            "custom_node.duplicate_registry_id",
        ),
    ]


# Downloader, build, and file validation protect host/runtime transfer settings.
@pytest.mark.parametrize("downloader", ["aria2", "httpx"])
def test_supported_global_downloaders_are_valid(downloader: str) -> None:
    """Accept both configured downloader backends."""
    config = make_config()
    config.cdh.default_downloader = downloader

    assert validate_config(config) == ()


def test_global_downloader_enum_and_numeric_ranges_are_validated() -> None:
    """Reject unknown backends and impossible downloader numeric settings."""
    config = make_config()
    config.cdh.default_downloader = "curl"
    config.cdh.downloader.aria2.rpc_port = -1
    config.cdh.downloader.aria2.split = 0
    config.cdh.downloader.aria2.max_connection_per_server = 0
    config.cdh.downloader.httpx.timeout = -0.5
    config.cdh.downloader.httpx.retries = -3

    assert locations_and_codes(validate_config(config)) == [
        (("cdh", "default_downloader"), "cdh.unsupported_default_downloader"),
        (
            ("cdh", "downloader", "httpx", "timeout"),
            "cdh.downloader.httpx_timeout_not_positive",
        ),
        (
            ("cdh", "downloader", "httpx", "retries"),
            "cdh.downloader.httpx_retries_negative",
        ),
        (
            ("cdh", "downloader", "aria2", "rpc_port"),
            "cdh.downloader.aria2_rpc_port_out_of_range",
        ),
        (
            ("cdh", "downloader", "aria2", "split"),
            "cdh.downloader.aria2_split_not_positive",
        ),
        (
            ("cdh", "downloader", "aria2", "max_connection_per_server"),
            "cdh.downloader.aria2_max_connection_per_server_not_positive",
        ),
    ]


def test_async_download_mode_is_accepted_by_public_schema() -> None:
    """Accept runtime async scheduling fields in public config."""
    config = Config.model_validate(
        {
            "compute_platform": {
                "type": "cuda",
                "cuda": {"version": "12.9.2"},
            },
            "pytorch": {"version": "2.10"},
            "cdh": {"default_download_mode": "async"},
            "comfyui": {"version": "latest"},
            "files": [
                {
                    "url": "https://example.com/model.bin",
                    "dir": "models",
                    "filename": "model.bin",
                    "download_mode": "async",
                }
            ],
        }
    )

    assert config.cdh.default_download_mode == "async"
    assert config.files[0].download_mode == "async"


def test_invalid_download_mode_is_rejected_by_public_schema() -> None:
    """Reject ordinary invalid runtime scheduling enum values."""
    with pytest.raises(ValidationError) as raised:
        Config.model_validate(
            {
                "compute_platform": {
                    "type": "cuda",
                    "cuda": {"version": "12.9.2"},
                },
                "pytorch": {"version": "2.10"},
                "cdh": {"default_download_mode": "parallel"},
                "comfyui": {"version": "latest"},
                "files": [
                    {
                        "url": "https://example.com/model.bin",
                        "dir": "models",
                        "filename": "model.bin",
                        "download_mode": "deferred",
                    }
                ],
            }
        )

    assert {(error["loc"], error["type"]) for error in raised.value.errors()} == {
        (("cdh", "default_download_mode"), "literal_error"),
        (("files", 0, "download_mode"), "literal_error"),
    }


def test_unknown_sections_stay_hard_schema_errors() -> None:
    """Keep extra root sections out of host warning compatibility handling."""
    with pytest.raises(ValidationError) as raised:
        Config.model_validate(
            {
                "compute_platform": {
                    "type": "cuda",
                    "cuda": {"version": "12.9.2"},
                },
                "pytorch": {"version": "2.10"},
                "comfyui": {"version": "latest"},
                "runtime": {"download_mode": "sync"},
            }
        )

    assert (("runtime",), "extra_forbidden") in {
        (error["loc"], error["type"]) for error in raised.value.errors()
    }


@pytest.mark.parametrize("port", [1, 65535])
def test_aria2_rpc_port_accepts_valid_tcp_port_range(port: int) -> None:
    """Allow every valid TCP port, including privileged ports."""
    config = make_config()
    config.cdh.downloader.aria2.rpc_port = port

    assert validate_config(config) == ()


def test_build_tags_accept_non_empty_image_references() -> None:
    """Accept one or more configured Docker image tags for host build."""
    config = make_config()
    config.build.tags = ["my-comfy:dev", "registry.example.com/team/my-comfy:dev"]

    assert validate_config(config) == ()


@pytest.mark.parametrize(
    "tag",
    ["", " my-comfy:dev", "my-comfy:dev ", "bad tag:dev", "bad\ntag", "bad\x7ftag"],
)
def test_build_tags_reject_empty_whitespace_and_control_characters(tag: str) -> None:
    """Reject tag strings that are obviously not valid CLI tag arguments."""
    config = make_config()
    config.build.tags = ["valid:tag", tag]

    assert locations_and_codes(validate_config(config)) == [
        (("build", "tags", 1), "build.invalid_tag")
    ]


@pytest.mark.parametrize("output", ["load", "push"])
def test_build_output_accepts_load_and_push(output: str) -> None:
    """Accept the supported Buildx output modes."""
    config = make_config()
    config.build.output = output

    assert validate_config(config) == ()


def test_build_output_rejects_unknown_modes_structurally() -> None:
    """Reject unsupported Buildx output modes in the public schema stage."""
    with pytest.raises(ValidationError) as raised:
        Config.model_validate(
            {
                "compute_platform": {
                    "type": "cuda",
                    "cuda": {"version": "12.9.2"},
                },
                "pytorch": {"version": "2.10"},
                "build": {"output": "registry"},
                "comfyui": {"version": "latest"},
            }
        )

    assert (("build", "output"), "literal_error") in {
        (error["loc"], error["type"]) for error in raised.value.errors()
    }


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/model",
        "model.safetensors",
        "https:///model",
        "https://",
        "https://example.com:bad/model",
        "https://example.com\\evil/model",
    ],
)
def test_file_urls_must_be_http_or_https_with_a_host(url: str) -> None:
    """Reject non-HTTP(S), relative, and hostless file URLs."""
    config = make_config()
    config.files = [FileConfig(url=url, dir="models", filename="model.bin")]

    assert locations_and_codes(validate_config(config)) == [
        (("files", 0, "url"), "file.invalid_url")
    ]


@pytest.mark.parametrize("url", ["http://example.com/a", "HTTPS://example.com/a"])
def test_http_and_https_file_urls_are_valid(url: str) -> None:
    """Treat URI schemes case-insensitively and accept both supported schemes."""
    config = make_config()
    config.files = [FileConfig(url=url, dir="models", filename="model.bin")]

    assert validate_config(config) == ()


def test_file_directories_follow_runtime_lexical_rules() -> None:
    """Reject dirs that runtime config would reject before baking."""
    config = make_config()
    config.files = [
        FileConfig(url="https://example.com/a", dir="/models", filename="a.bin"),
        FileConfig(
            url="https://example.com/b",
            dir="models/../outside",
            filename="b.bin",
        ),
        FileConfig(url="https://example.com/c", dir="", filename="c.bin"),
        FileConfig(url="https://example.com/d", dir=".", filename="d.bin"),
        FileConfig(url="https://example.com/e", dir="models/", filename="e.bin"),
        FileConfig(
            url="https://example.com/f",
            dir="models//checkpoints",
            filename="f.bin",
        ),
    ]

    assert locations_and_codes(validate_config(config)) == [
        (("files", 0, "dir"), "file.absolute_directory"),
        (("files", 1, "dir"), "file.directory_traversal"),
        (("files", 2, "dir"), "file.empty_directory_segment"),
        (("files", 3, "dir"), "file.current_directory_segment"),
        (("files", 4, "dir"), "file.trailing_slash"),
        (("files", 5, "dir"), "file.empty_directory_segment"),
    ]


@pytest.mark.parametrize("filename", ["", ".", "..", "a/b", "a\\b"])
def test_explicit_filenames_are_safe_single_components(filename: str) -> None:
    """Reject empty, special, and separated explicit filenames."""
    config = make_config()
    config.files = [
        FileConfig(url="https://example.com/model", dir="models", filename=filename)
    ]

    assert locations_and_codes(validate_config(config)) == [
        (("files", 0, "filename"), "file.invalid_filename")
    ]


def test_file_filename_is_required_by_public_schema() -> None:
    """Require explicit file targets before business validation."""
    with pytest.raises(ValidationError) as raised:
        Config.model_validate(
            {
                "compute_platform": {
                    "type": "cuda",
                    "cuda": {"version": "12.9.2"},
                },
                "pytorch": {"version": "2.10"},
                "comfyui": {"version": "latest"},
                "files": [
                    {"url": "https://example.com/model.bin", "dir": "models"},
                ],
            }
        )

    assert (("files", 0, "filename"), "missing") in {
        (error["loc"], error["type"]) for error in raised.value.errors()
    }


def test_per_file_downloader_enum_is_validated() -> None:
    """Reject an unsupported explicit per-file backend."""
    config = make_config()
    config.files = [
        FileConfig(
            url="https://example.com/model",
            dir="models",
            filename="model.bin",
            downloader="curl",
        )
    ]

    assert locations_and_codes(validate_config(config)) == [
        (("files", 0, "downloader"), "file.unsupported_downloader")
    ]


def test_independent_business_errors_are_aggregated_in_config_order() -> None:
    """Return all discoverable business errors in deterministic schema order."""
    config = make_config()
    config.compute_platform.type = "rocm"
    config.compute_platform.cuda.version = "bad"
    config.compute_platform.cuda.image_flavor = "bad"
    config.compute_platform.cuda.image_distro = "bad"
    config.system.workspace = "relative"
    config.system.env = {"PATH": "override"}
    config.pytorch.extra_packages = ["torch"]
    config.comfyui.version = "bad"
    config.comfyui.cli_version = "~=1"
    config.cdh.default_downloader = "curl"
    config.files = [
        FileConfig(
            url="ftp://example.com/",
            dir="../outside",
            filename="model.bin",
            downloader="curl",
        )
    ]

    assert locations_and_codes(validate_config(config)) == [
        (("compute_platform", "type"), "compute_platform.unsupported_backend"),
        (
            ("compute_platform", "cuda", "version"),
            "compute_platform.invalid_cuda_version",
        ),
        (
            ("compute_platform", "cuda", "image_flavor"),
            "compute_platform.unsupported_image_flavor",
        ),
        (
            ("compute_platform", "cuda", "image_distro"),
            "compute_platform.unsupported_image_distro",
        ),
        (("system", "workspace"), "system.path_not_absolute"),
        (("system", "env", "PATH"), "system.managed_env_override"),
        (("pytorch", "extra_packages", 0), "pytorch.duplicate_torch"),
        (("comfyui", "version"), "comfyui.invalid_version"),
        (("comfyui", "cli_version"), "comfyui.invalid_cli_version"),
        (("cdh", "default_downloader"), "cdh.unsupported_default_downloader"),
        (("files", 0, "url"), "file.invalid_url"),
        (("files", 0, "dir"), "file.directory_traversal"),
        (("files", 0, "downloader"), "file.unsupported_downloader"),
    ]
