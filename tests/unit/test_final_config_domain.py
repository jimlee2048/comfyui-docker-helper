"""Public-configuration boundary contracts."""

from pathlib import Path
from typing import Any

import pytest

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticError,
    DiagnosticSeverity,
)
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_validation import (
    FinalConfigError,
    validate_final_config_domains,
    validate_final_config_semantics,
    validate_final_config_structure,
)


def _document() -> dict[str, Any]:
    return {
        "compute_platform": {"type": "cuda", "cuda": {"version": "13.0.3"}},
        "pytorch": {"version": "2.12.1"},
        "comfyui": {"version": "0.11.0", "install_manager": False},
    }


def _credential_document() -> dict[str, Any]:
    document = _document()
    document["secrets"] = {"private_git": {"env": "CDH_PRIVATE_GIT_TOKEN"}}
    document["cdh"] = {
        "git": {
            "credentials": [
                {
                    "match": "https://github.com/acme/",
                    "username": "x-access-token",
                    "password": {"secret": "private_git"},
                }
            ]
        }
    }
    return document


_PRIVATE_SECRET_PATH = ("secrets", "private_git")
_CREDENTIAL_PATH = ("cdh", "git", "credentials", 0)
_CREDENTIAL_SECRET_PATH = (*_CREDENTIAL_PATH, "password", "secret")
_VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "first@example"
)


def _codes(config: FinalConfig, *, build_hooks_dir: Path | None = None) -> set[str]:
    return {
        diagnostic.code
        for diagnostic in _diagnostics(config, build_hooks_dir=build_hooks_dir)
    }


def _diagnostics(
    config: FinalConfig,
    *,
    build_hooks_dir: Path | None = None,
) -> tuple[Diagnostic, ...]:
    domains = validate_final_config_domains(config, build_hooks_dir=build_hooks_dir)
    return (*domains.diagnostics, *validate_final_config_semantics(config, domains))


# Final configuration admits strict public types and enforces cross-field ownership.
def test_final_structure_uses_exact_baseline_defaults() -> None:
    config = validate_final_config_structure(_document())

    assert config.python.version == "3.13.14"
    assert config.python.uv_version == "latest"
    assert config.build.platforms == ["linux/amd64"]
    assert config.compute_platform.cuda.image_flavor == "cudnn-devel"
    assert config.compute_platform.cuda.image_distro == "ubuntu24.04"
    assert config.comfyui.install_cli is True
    assert _diagnostics(config) == ()


def test_secret_sources_and_git_credentials_use_typed_complete_values() -> None:
    config = validate_final_config_structure(_credential_document())

    assert config.secrets["private_git"].env == "CDH_PRIVATE_GIT_TOKEN"
    route = config.cdh.git.credentials[0]
    assert route.password.secret == "private_git"
    assert _diagnostics(config) == ()

    document = _credential_document()
    document["cdh"]["git"]["credentials"][0]["password"] = "plaintext-token"
    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)
    assert raised.value.diagnostics[0].path == (
        "cdh",
        "git",
        "credentials",
        0,
        "password",
    )


@pytest.mark.parametrize(
    "locator",
    [
        "token",
        "tokens/private git token",
        "./token",
        "../token",
        "tokens/../token",
        "/run/secrets/token",
    ],
)
def test_secret_file_locators_accept_posix_file_spellings(locator: str) -> None:
    document = _credential_document()
    document["secrets"]["private_git"] = {"file": locator}
    config = validate_final_config_structure(document)

    assert _diagnostics(config) == ()


@pytest.mark.parametrize(
    "locator",
    [
        "",
        "token\x00file",
        "token\nfile",
        "tokens\\private",
        "tokens/",
        "//server/token",
        ".",
        "..",
        "tokens/.",
        "tokens/..",
    ],
)
def test_secret_file_locators_reject_non_file_spellings(locator: str) -> None:
    document = _credential_document()
    document["secrets"]["private_git"] = {"file": locator}
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("secrets", "private_git", "file"),
            "secret.invalid_file",
            DiagnosticSeverity.ERROR,
        )
    ]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            "invalid-secret-name",
            (
                (("secrets", "Bad.Name"), "secret.invalid_name"),
                (_CREDENTIAL_SECRET_PATH, "secret.unknown_reference"),
            ),
        ),
        (
            "missing-source",
            ((_PRIVATE_SECRET_PATH, "secret.invalid_source"),),
        ),
        (
            "two-sources",
            ((_PRIVATE_SECRET_PATH, "secret.invalid_source"),),
        ),
        (
            "invalid-env",
            (((*_PRIVATE_SECRET_PATH, "env"), "secret.invalid_env"),),
        ),
        (
            "invalid-reference",
            ((_CREDENTIAL_SECRET_PATH, "secret.invalid_reference"),),
        ),
        (
            "unknown-reference",
            ((_CREDENTIAL_SECRET_PATH, "secret.unknown_reference"),),
        ),
        (
            "empty-username",
            (((*_CREDENTIAL_PATH, "username"), "git_credential.invalid_username"),),
        ),
        (
            "query-match",
            (((*_CREDENTIAL_PATH, "match"), "git_credential.invalid_match"),),
        ),
        (
            "password-userinfo",
            (
                (
                    (*_CREDENTIAL_PATH, "match"),
                    "git_credential.password_userinfo_forbidden",
                ),
            ),
        ),
    ],
)
def test_secret_and_credential_domains_report_exact_diagnostics(
    mutation: str,
    expected: tuple[tuple[tuple[str | int, ...], str], ...],
) -> None:
    document = _credential_document()
    source = document["secrets"]["private_git"]
    route = document["cdh"]["git"]["credentials"][0]
    if mutation == "invalid-secret-name":
        document["secrets"] = {"Bad.Name": source}
    elif mutation == "missing-source":
        source.clear()
    elif mutation == "two-sources":
        source["file"] = "token-file"
    elif mutation == "invalid-env":
        source["env"] = "9INVALID"
    elif mutation == "invalid-reference":
        route["password"]["secret"] = "Bad.Name"
    elif mutation == "unknown-reference":
        route["password"]["secret"] = "not_defined"
    elif mutation == "empty-username":
        route["username"] = ""
    elif mutation == "query-match":
        route["match"] = "https://github.com/acme/?"
    else:
        route["match"] = "https://user:synthetic-marker@github.com/acme/"

    config = validate_final_config_structure(document)
    diagnostics = _diagnostics(config)

    assert tuple(
        (item.path, item.code, item.severity) for item in diagnostics
    ) == tuple((path, code, DiagnosticSeverity.ERROR) for path, code in expected)
    if mutation == "password-userinfo":
        assert all("synthetic-marker" not in item.message for item in diagnostics)


def test_git_credential_context_duplicates_and_http_warnings_are_semantic() -> None:
    document = _credential_document()
    document["cdh"]["git"]["credentials"] = [
        {
            "match": "http://EXAMPLE.com:80/team/",
            "username": "first",
            "password": {"secret": "private_git"},
        },
        {
            "match": "http://example.com/team",
            "username": "second",
            "password": {"secret": "private_git"},
        },
        {
            "match": "https://example.com/other/",
            "username": "secure",
            "password": {"secret": "private_git"},
        },
    ]
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("cdh", "git", "credentials", 0, "match"),
            "git_credential.insecure_http",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("cdh", "git", "credentials", 1, "match"),
            "git_credential.insecure_http",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("cdh", "git", "credentials", 1, "match"),
            "git_credential.duplicate_match",
            DiagnosticSeverity.ERROR,
        ),
    ]


def test_git_credential_username_uses_the_protocol_utf8_byte_limit() -> None:
    document = _credential_document()
    route = document["cdh"]["git"]["credentials"][0]
    route["username"] = "é" * 32_762 + "a"
    maximum = validate_final_config_structure(document)

    assert "git_credential.invalid_username" not in _codes(maximum)
    route["username"] += "a"
    oversized = validate_final_config_structure(document)
    diagnostics = _diagnostics(oversized)

    assert [(item.path, item.code) for item in diagnostics] == [
        (
            ("cdh", "git", "credentials", 0, "username"),
            "git_credential.invalid_username",
        )
    ]


def test_direct_git_password_userinfo_is_rejected_but_username_only_is_valid() -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [
        {
            "type": "git",
            "url": "https://alice@example.com/public.git",
        },
        {
            "type": "git",
            "url": "https://alice:synthetic-marker@example.com/private.git",
        },
    ]
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [item.code for item in diagnostics] == [
        "custom_node.password_userinfo_forbidden"
    ]
    assert "synthetic-marker" not in diagnostics[0].message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_flavor", "cudnn"),
        ("image_flavor", 1),
        ("image_distro", "ubuntu20.04"),
        ("image_distro", 24),
    ],
)
def test_cuda_image_selectors_reject_values_outside_the_public_enums(
    field: str,
    value: object,
) -> None:
    document = _document()
    document["compute_platform"]["cuda"][field] = value

    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)

    assert raised.value.diagnostics[0].path == (
        "compute_platform",
        "cuda",
        field,
    )
    assert raised.value.diagnostics[0].code == "schema.literal_error"


@pytest.mark.parametrize("value", [0, 1, "true", "false"])
def test_install_cli_is_a_strict_boolean(value: object) -> None:
    document = _document()
    document["comfyui"]["install_cli"] = value

    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)

    assert raised.value.diagnostics[0].path == ("comfyui", "install_cli")


def test_domain_and_semantic_passes_are_isolated_and_ordered() -> None:
    document = _document()
    document["compute_platform"]["cuda"]["version"] = "bad"
    document["build"] = {"platforms": ["linux/amd64", "linux/amd64"]}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    semantics = validate_final_config_semantics(config, domains)

    assert [item.code for item in domains.diagnostics] == [
        "compute_platform.invalid_cuda_version"
    ]
    assert [item.code for item in semantics] == ["build.duplicate_platform"]
    assert _diagnostics(config) == (*domains.diagnostics, *semantics)


def test_uv_tools_are_active_strict_isolated_requirements() -> None:
    document = _document()
    document["python"] = {"uv_tools": ["Ruff==0.15.18", "mypy[dmypy]>=1,<2"]}

    config = validate_final_config_structure(document)

    assert config.python.uv_tools == ["Ruff==0.15.18", "mypy[dmypy]>=1,<2"]
    assert _diagnostics(config) == ()


@pytest.mark.parametrize(
    "uv_tools",
    [
        ["ruff", "Ruff==0.15.18"],
        ["comfyui-docker-helper==0.5.0"],
        ["Comfy_CLI>=1.7"],
        ["demo @ https://example.com/demo.whl"],
    ],
)
def test_uv_tools_reject_duplicate_reserved_or_direct_sources(
    uv_tools: list[str],
) -> None:
    document = _document()
    document["python"] = {"uv_tools": uv_tools}
    config = validate_final_config_structure(document)

    assert _diagnostics(config)


@pytest.mark.parametrize("install_cli", [True, False])
def test_comfy_cli_generic_tool_owner_is_reserved_in_both_modes(
    install_cli: bool,
) -> None:
    document = _document()
    document["comfyui"]["install_cli"] = install_cli
    document["python"] = {"uv_tools": ["Comfy_CLI"]}
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [item.code for item in diagnostics] == ["python.duplicate_package_owner"]
    assert diagnostics[0].path == ("python", "uv_tools", 0)
    assert diagnostics[0].message == (
        "package comfy-cli is reserved by comfyui.install_cli"
    )


@pytest.mark.parametrize("install_cli", [True, False])
@pytest.mark.parametrize("group", ["python", "pytorch"])
def test_comfy_cli_application_owner_is_reserved_in_every_direct_group(
    install_cli: bool,
    group: str,
) -> None:
    document = _document()
    document["comfyui"]["install_cli"] = install_cli
    document.setdefault(group, {})["extra_packages"] = ["Comfy_CLI>=1.7,<2"]
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [item.code for item in diagnostics] == ["python.duplicate_package_owner"]
    assert diagnostics[0].path == (group, "extra_packages", 0)
    assert diagnostics[0].message == (
        "package comfy-cli is reserved by comfyui.install_cli"
    )


@pytest.mark.parametrize(
    "name",
    [
        "UV_CONSTRAINT",
        "UV_INDEX",
        "UV_CONFIG_FILE",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "PIP_CONSTRAINT",
        "PIP_INDEX_URL",
        "PIP_CONFIG_FILE",
    ],
)
def test_system_env_rejects_package_authority_controls(name: str) -> None:
    document = _document()
    document["system"] = {"env": {name: "user-value"}}
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [item.code for item in diagnostics] == ["system.managed_env_override"]
    assert diagnostics[0].path == ("system", "env", name)


def test_system_env_preserves_non_package_runtime_values() -> None:
    document = _document()
    document["system"] = {
        "env": {
            "APP_PROFILE": "production",
            "PIPER_MODE": "fast",
            "UVICORN_WORKERS": "2",
        }
    }
    config = validate_final_config_structure(document)

    assert _diagnostics(config) == ()
    assert config.system.env == {
        "APP_PROFILE": "production",
        "PIPER_MODE": "fast",
        "UVICORN_WORKERS": "2",
    }


# Public OS-package diagnostics cover the effective default-plus-user set and
# enforce canonical lowercase Debian identities before planning.
@pytest.mark.parametrize("package", ["bash", "tini"])
def test_system_extra_package_warns_and_filters_default_overlap(package: str) -> None:
    document = _document()
    document["system"] = {"extra_packages": [package]}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    diagnostics = (
        *domains.diagnostics,
        *validate_final_config_semantics(config, domains),
    )

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("system", "extra_packages", 0),
            "system.redundant_default_apt_package",
            DiagnosticSeverity.WARNING,
        )
    ]
    assert domains.authored_apt_packages[0].value == package
    assert domains.apt_packages == ()


@pytest.mark.parametrize("package", ["bash", "libexample"])
def test_system_extra_package_keeps_user_duplicate_as_error(package: str) -> None:
    document = _document()
    document["system"] = {"extra_packages": [package, package]}
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [(item.path, item.code) for item in diagnostics] == [
        (("system", "extra_packages", 1), "system.duplicate_apt_package")
    ]


@pytest.mark.parametrize("package", ["Bash", "x"])
def test_system_extra_package_rejects_noncanonical_debian_name(package: str) -> None:
    document = _document()
    document["system"] = {"extra_packages": [package]}
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [(item.path, item.code) for item in diagnostics] == [
        (("system", "extra_packages", 0), "system.invalid_apt_package")
    ]


def test_system_extra_package_accepts_lowercase_debian_punctuation() -> None:
    document = _document()
    document["system"] = {"extra_packages": ["libfoo+bar.1-dev"]}
    config = validate_final_config_structure(document)

    assert _diagnostics(config) == ()


def test_host_ssh_public_keys_normalize_and_warn_by_key_identity() -> None:
    duplicate = _VALID_SSH_KEY.rsplit(" ", 1)[0] + " second@example"
    authored = ["  ", f"  {_VALID_SSH_KEY}  ", duplicate]
    document = _document()
    document["system"] = {"ssh": {"pub_keys": authored}}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert [(item.path, item.code, item.severity) for item in domains.diagnostics] == [
        (
            ("system", "ssh", "pub_keys", 2),
            "ssh.redundant_public_key",
            DiagnosticSeverity.WARNING,
        )
    ]
    assert domains.ssh_public_keys == (_VALID_SSH_KEY,)
    assert config.system.ssh.pub_keys == authored
    assert domains.diagnostics[0].source_context is None
    assert _VALID_SSH_KEY not in domains.diagnostics[0].message


# Structural and scalar domains reject coercion, ambiguity, and unsupported values.
def test_strict_structure_forbids_unknown_fields_and_coercion() -> None:
    document = _document()
    document["build"] = {"platforms": ["linux/amd64"], "unknown": True}
    document["comfyui"]["port"] = "8188"

    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)

    assert [(item.path, item.code) for item in raised.value.diagnostics] == [
        (("build", "unknown"), "schema.extra_forbidden"),
        (("comfyui", "port"), "schema.int_type"),
    ]


@pytest.mark.parametrize("version", ["3.13", "3.13.14rc1", "latest", " 3.13.14"])
def test_python_requires_an_exact_stable_patch(version: str) -> None:
    document = _document()
    document["python"] = {"version": version}
    config = validate_final_config_structure(document)

    assert _codes(config) & {
        "python.exact_patch_required",
        "python.stable_version_required",
    }


@pytest.mark.parametrize("version", ["3.11.9", "3.15.0"])
def test_python_rejects_versions_outside_package_support(version: str) -> None:
    document = _document()
    document["python"] = {"version": version}
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [(item.path, item.code) for item in diagnostics] == [
        (("python", "version"), "python.unsupported_version")
    ]


def test_python_accepts_unlisted_patch_inside_package_support() -> None:
    document = _document()
    document["python"] = {"version": "3.13.15"}
    config = validate_final_config_structure(document)

    assert _diagnostics(config) == ()


@pytest.mark.parametrize("selector", ["0.11.28", "latest"])
def test_uv_release_selector_accepts_exact_or_rolling_authority(selector: str) -> None:
    document = _document()
    document["python"] = {"uv_version": selector}
    config = validate_final_config_structure(document)

    assert _diagnostics(config) == ()


@pytest.mark.parametrize(
    "selector",
    [
        "0.11",
        "0.11.28rc1",
        "v0.11.28",
        "0.11.28-debian-slim",
        "debian-slim",
        "custom",
        "bad/tag",
    ],
)
def test_uv_release_selector_rejects_non_release_provider_tags(selector: str) -> None:
    document = _document()
    document["python"] = {"uv_version": selector}
    config = validate_final_config_structure(document)

    assert "python.invalid_uv_version" in _codes(config)


@pytest.mark.parametrize("version", ["2.12", "2.12.1rc1", "latest", "v2.12.1"])
def test_pytorch_requires_an_exact_stable_public_version(version: str) -> None:
    document = _document()
    document["pytorch"]["version"] = version
    config = validate_final_config_structure(document)

    assert "pytorch.exact_stable_version_required" in _codes(config)


@pytest.mark.parametrize(
    ("section", "field", "value", "code"),
    [
        ("python", "index_url", "ftp://example.com/simple", "python.invalid_index_url"),
        (
            "pytorch",
            "index_base_url",
            "https://",
            "pytorch.invalid_index_base_url",
        ),
        ("python", "uv_version", "bad/tag", "python.invalid_uv_version"),
    ],
)
def test_planning_execution_strings_have_consumer_aligned_domains(
    section: str,
    field: str,
    value: str,
    code: str,
) -> None:
    document = _document()
    document.setdefault(section, {})[field] = value
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert code in {item.code for item in domains.diagnostics}


@pytest.mark.parametrize("value", ["", " ", "1M\nprobe", "-1M"])
def test_aria2_min_split_size_rejects_ambiguous_argv_values(value: str) -> None:
    document = _document()
    document["cdh"] = {"downloader": {"aria2": {"min_split_size": value}}}
    config = validate_final_config_structure(document)

    assert "cdh.downloader.invalid_aria2_min_split_size" in _codes(config)


def test_platforms_are_nonempty_typed_and_duplicate_free() -> None:
    document = _document()
    document["build"] = {"platforms": []}
    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)
    assert raised.value.diagnostics[0].path == ("build", "platforms")

    document["build"] = {"platforms": ["linux/arm64"]}
    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)
    assert raised.value.diagnostics[0].code == "schema.literal_error"

    document["build"] = {"platforms": ["linux/amd64", "linux/amd64"]}
    config = validate_final_config_structure(document)
    assert "build.duplicate_platform" in _codes(config)


# ComfyUI and Registry selectors preserve stable supported release identities.
@pytest.mark.parametrize("version", ["0.3.60", "v0.3.60", "<0.11.0", "==0.3.60"])
def test_comfyui_rejects_selectors_definitely_below_floor(version: str) -> None:
    document = _document()
    document["comfyui"]["version"] = version
    config = validate_final_config_structure(document)

    assert "comfyui.version_below_floor" in _codes(config)


@pytest.mark.parametrize(
    "version",
    [
        "0.11.0",
        "v0.11.1",
        ">=0.11.0,<1",
        "latest",
        "nightly",
        "09725967cf76304371c390ca1d6483e04061da48",
    ],
)
def test_comfyui_accepts_floor_compatible_stable_selectors(version: str) -> None:
    document = _document()
    document["comfyui"]["version"] = version
    config = validate_final_config_structure(document)

    assert "comfyui.version_below_floor" not in _codes(config)


@pytest.mark.parametrize(
    ("version", "code"),
    [
        ("", "comfyui.invalid_version"),
        (">=0.12.0,<0.12.0", "comfyui.unsatisfiable_selector"),
        (">0.12.0,<=0.12.0", "comfyui.unsatisfiable_selector"),
        (">0.12.0,<0.12.1", "comfyui.unsatisfiable_selector"),
        ("==0.12.0,!=0.12.0", "comfyui.unsatisfiable_selector"),
        ("0.12.0-rc.1", "comfyui.formal_stable_release_required"),
        ("0.12.0+local", "comfyui.formal_stable_release_required"),
        (">=0.12.0rc1,<1", "comfyui.prerelease_selector_forbidden"),
    ],
)
def test_comfyui_requires_a_satisfiable_stable_formal_selector(
    version: str,
    code: str,
) -> None:
    document = _document()
    document["comfyui"]["version"] = version
    config = validate_final_config_structure(document)

    assert code in _codes(config)


def test_comfyui_selector_satisfiability_uses_discrete_formal_releases() -> None:
    document = _document()
    document["comfyui"]["version"] = ">=0.12.0,<0.12.1"
    config = validate_final_config_structure(document)

    assert "comfyui.unsatisfiable_selector" not in _codes(config)


def test_build_tags_admit_dynamic_publication_expressions() -> None:
    document = _document()
    document["build"] = {
        "tags": [
            "example/image:v${{ comfyui.release }}",
            "example/image:${{ comfyui.commit }}",
            "example/image:custom-${{ comfyui.commit.prefix(12) }}",
        ]
    }
    config = validate_final_config_structure(document)

    assert _diagnostics(config) == ()


@pytest.mark.parametrize(
    "selector", ["nightly", "09725967cf76304371c390ca1d6483e04061da48"]
)
def test_release_expression_rejects_selectors_without_formal_releases(
    selector: str,
) -> None:
    document = _document()
    document["comfyui"]["version"] = selector
    document["build"] = {"tags": ["example/image:v${{ comfyui.release }}"]}
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [(item.path, item.code) for item in diagnostics] == [
        (("build", "tags", 0), "build.release_unavailable")
    ]


def test_invalid_comfyui_selector_does_not_cascade_release_diagnostic() -> None:
    document = _document()
    document["comfyui"]["version"] = "not a selector"
    document["build"] = {"tags": ["example/image:v${{ comfyui.release }}"]}
    config = validate_final_config_structure(document)

    assert [item.code for item in _diagnostics(config)] == ["comfyui.invalid_version"]


def test_registry_nodes_require_manager_but_direct_git_nodes_do_not() -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [
        {"type": "git", "url": "https://github.com/example/direct.git"}
    ]
    git_config = validate_final_config_structure(document)
    assert "custom_node.manager_required" not in _codes(git_config)

    document["comfyui"]["custom_nodes"] = [
        {"type": "registry", "id": "example-node", "version": "1.0.0"}
    ]
    registry_config = validate_final_config_structure(document)
    diagnostics = _diagnostics(registry_config)
    assert [
        item.path for item in diagnostics if item.code == "custom_node.manager_required"
    ] == [("comfyui", "custom_nodes", 0, "type")]


def test_exact_registry_prerelease_remains_a_valid_published_selector() -> None:
    document = _document()
    document["comfyui"]["install_manager"] = True
    document["comfyui"]["custom_nodes"] = [
        {"type": "registry", "id": "example", "version": "1.0.0-rc.1"}
    ]
    config = validate_final_config_structure(document)

    assert "custom_node.invalid_registry_version" not in _codes(config)


def test_registry_punctuation_variants_report_distribution_identity_collision() -> None:
    document = _document()
    document["comfyui"]["install_manager"] = True
    document["comfyui"]["custom_nodes"] = [
        {"type": "registry", "id": "Example_Node", "version": "1.0.0"},
        {"type": "registry", "id": "example.node", "version": "1.1.0"},
    ]
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert config.comfyui.custom_nodes[0].id == "Example_Node"
    assert [
        item.path
        for item in diagnostics
        if item.code == "custom_node.registry_distribution_identity_collision"
    ] == [("comfyui", "custom_nodes", 1, "id")]


@pytest.mark.parametrize("node_id", ["invalid/name", "invalid!name"])
def test_registry_id_requires_valid_project_name(node_id: str) -> None:
    document = _document()
    document["comfyui"]["install_manager"] = True
    document["comfyui"]["custom_nodes"] = [
        {"type": "registry", "id": node_id, "version": "1.0.0"}
    ]
    config = validate_final_config_structure(document)

    assert "custom_node.invalid_registry_id" in _codes(config)


def test_registry_selector_ranges_reject_prerelease_operands() -> None:
    document = _document()
    document["comfyui"]["install_manager"] = True
    document["comfyui"]["custom_nodes"] = [
        {"type": "registry", "id": "example", "version": ">=1.0.0-rc.1,<2"}
    ]
    config = validate_final_config_structure(document)

    assert "custom_node.invalid_registry_version" in _codes(config)


# Package ownership and requirement normalization prevent cross-source ambiguity.
def test_package_ownership_is_normalized_across_groups() -> None:
    document = _document()
    document["python"] = {"extra_packages": ["My_Package[cli]>=1,<2"]}
    document["pytorch"]["extra_packages"] = ["my-package==1.5", "torch==2.12.1"]
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    domains = validate_final_config_domains(config)
    assert "python.duplicate_package_owner" not in {
        item.code for item in domains.diagnostics
    }
    assert "python.duplicate_package_owner" in {
        item.code for item in validate_final_config_semantics(config, domains)
    }

    assert [
        item.path
        for item in diagnostics
        if item.code == "python.conflicting_package_requirement"
    ] == [("pytorch", "extra_packages", 0)]
    assert [
        item.path
        for item in diagnostics
        if item.code == "python.duplicate_package_owner"
    ] == [("pytorch", "extra_packages", 1)]


@pytest.mark.parametrize(
    "requirement",
    [
        "Torch==2.12.1",
        "TorchVision==0.27.1",
        "TorchAudio==2.11.0",
        "PIP==26.1.2",
        "Setuptools==81.0.0",
    ],
)
def test_python_extras_reject_reserved_application_package_owners(
    requirement: str,
) -> None:
    document = _document()
    document["python"] = {"extra_packages": [requirement]}
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [(item.path, item.code) for item in diagnostics] == [
        (("python", "extra_packages", 0), "python.duplicate_package_owner")
    ]


def test_package_ownership_is_scoped_to_isolated_environment() -> None:
    document = _document()
    document["python"] = {
        "extra_packages": ["ruff==0.15.18"],
        "uv_tools": ["Ruff==0.15.18"],
    }
    config = validate_final_config_structure(document)

    assert _diagnostics(config) == ()


@pytest.mark.parametrize(
    ("requirement", "code"),
    [
        ("demo @ https://example.com/demo.whl", "python.direct_requirement_forbidden"),
        ("demo>=1rc1,<2", "python.prerelease_selector_forbidden"),
        ("demo==1,==2", "python.ambiguous_exact_requirement"),
        ("demo==1,!=1", "python.ambiguous_exact_requirement"),
        ("demo===1", "python.unsupported_requirement_selector"),
        ("demo==1.*", "python.unsupported_requirement_selector"),
        (" demo==1", "python.invalid_requirement"),
    ],
)
def test_python_requirement_domain_rejects_ambiguous_inputs(
    requirement: str,
    code: str,
) -> None:
    document = _document()
    document["python"] = {"extra_packages": [requirement]}
    config = validate_final_config_structure(document)

    assert code in _codes(config)


@pytest.mark.parametrize(
    ("requirement", "specifier"),
    [
        ("demo", ""),
        ("demo==1", "==1"),
        ("demo!=1", "!=1"),
        ("demo<2", "<2"),
        ("demo<=2", "<=2"),
        ("demo>1", ">1"),
        ("demo>=1", ">=1"),
        ("demo~=1.2", "~=1.2"),
        ("demo==1,>=1", "==1,>=1"),
        ("demo>=2,<1", "<1,>=2"),
    ],
)
def test_python_requirement_domain_accepts_standard_direct_selectors(
    requirement: str,
    specifier: str,
) -> None:
    document = _document()
    document["python"] = {"extra_packages": [requirement]}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert domains.diagnostics == ()
    assert domains.package_requirements[0].specifier == specifier


@pytest.mark.parametrize("group", ["python", "pytorch"])
def test_direct_requirements_reject_environment_markers(group: str) -> None:
    document = _document()
    document.setdefault(group, {})["extra_packages"] = [
        'demo>=1,<2; python_version < "3.13"'
    ]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert [item.code for item in domains.diagnostics] == [
        "python.environment_marker_forbidden"
    ]
    assert domains.package_requirements == ()


def test_normalized_requirement_retains_every_resolution_affecting_input() -> None:
    document = _document()
    document["python"] = {"extra_packages": ["Demo[CLI]>=1,<2"]}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert domains.diagnostics == ()
    assert len(domains.package_requirements) == 1
    requirement = domains.package_requirements[0]
    assert requirement.name == "demo"
    assert requirement.extras == ("cli",)
    assert requirement.specifier == "<2,>=1"


@pytest.mark.parametrize("extra", ["Foo_Bar", "foo-bar", "FOO.BAR"])
def test_requirement_extras_use_pep685_identity(extra: str) -> None:
    document = _document()
    document["python"] = {"extra_packages": [f"Demo[{extra}]>=1,<2"]}
    config = validate_final_config_structure(document)

    requirement = validate_final_config_domains(config).package_requirements[0]

    assert requirement.extras == ("foo-bar",)


def test_requirement_extra_aliases_are_stably_deduplicated() -> None:
    document = _document()
    document["python"] = {
        "extra_packages": ["Demo[z_extra,Foo_Bar,foo-bar,FOO.BAR]>=1,<2"]
    }
    config = validate_final_config_structure(document)

    requirement = validate_final_config_domains(config).package_requirements[0]

    assert requirement.extras == ("foo-bar", "z-extra")


# File, Git, and hook inputs are unique, contained, and safe for their consumers.
def test_duplicate_file_targets_are_detected_after_path_normalization() -> None:
    document = _document()
    document["files"] = [
        {"url": "https://example.com/a", "dir": "models/x", "filename": "a.bin"},
        {"url": "https://example.com/b", "dir": "models/x", "filename": "a.bin"},
    ]
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [
        item.path for item in diagnostics if item.code == "file.duplicate_target"
    ] == [("files", 1, "filename")]


def test_file_target_rejects_only_the_exact_internal_staging_leaf() -> None:
    document = _document()
    document["files"] = [
        {
            "url": "https://example.com/reserved",
            "dir": "models",
            "filename": ".cdh-staging",
        },
        {
            "url": "https://example.com/ordinary",
            "dir": ".cdh-staging",
            "filename": ".cdh-staging.part",
        },
    ]
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [
        (item.path, item.code) for item in diagnostics if item.code.startswith("file.")
    ] == [(("files", 0, "filename"), "file.invalid_filename")]


def test_duplicate_effective_git_targets_are_rejected() -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [
        {"type": "git", "url": "https://github.com/a/one.git", "target_dir": "same"},
        {"type": "git", "url": "https://github.com/b/two.git", "target_dir": "same"},
    ]
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config)

    assert [
        item.path
        for item in diagnostics
        if item.code == "custom_node.duplicate_git_target_dir"
    ] == [("comfyui", "custom_nodes", 1, "target_dir")]


@pytest.mark.parametrize(
    "url",
    ["", "local/path", "https://", "file:///tmp/repo", "-ssh://host/repo"],
)
def test_git_source_url_requires_a_supported_remote_form(url: str) -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [{"type": "git", "url": url}]
    config = validate_final_config_structure(document)

    assert "custom_node.invalid_git_url" in _codes(config)


def test_invalid_git_url_does_not_suppress_independent_field_diagnostics() -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [
        {"type": "git", "url": "bad", "ref": "-bad", "target_dir": ".."}
    ]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert [(item.path, item.code) for item in domains.diagnostics] == [
        (
            ("comfyui", "custom_nodes", 0, "url"),
            "custom_node.invalid_git_url",
        ),
        (
            ("comfyui", "custom_nodes", 0, "ref"),
            "custom_node.invalid_git_ref",
        ),
        (
            ("comfyui", "custom_nodes", 0, "target_dir"),
            "custom_node.invalid_git_target_dir",
        ),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/repo.git",
        "ssh://git@github.com/example/repo.git",
        "git://github.com/example/repo.git",
        "git@github.com:example/repo.git",
    ],
)
def test_git_source_url_accepts_supported_remote_forms(url: str) -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [{"type": "git", "url": url}]
    config = validate_final_config_structure(document)

    assert "custom_node.invalid_git_url" not in _codes(config)


def test_hook_tree_preserves_order_and_requires_regular_non_symlink_files(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.sh"
    first.write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(first)
    document = _document()
    document["comfyui"]["custom_nodes"] = [
        {
            "type": "git",
            "url": "https://github.com/example/direct.git",
            "pre_install_hooks": ["first.sh", "linked.py"],
        }
    ]
    config = validate_final_config_structure(document)

    diagnostics = _diagnostics(config, build_hooks_dir=tmp_path)

    assert [
        item.path for item in diagnostics if item.code == "hook.source_not_regular"
    ] == [("comfyui", "custom_nodes", 0, "pre_install_hooks", 1)]
    assert config.comfyui.custom_nodes[0].pre_install_hooks == [
        "first.sh",
        "linked.py",
    ]


@pytest.mark.parametrize("ref", ["-main", "bad ref", "refs/../main", "name.lock"])
def test_git_refs_reject_ambiguous_or_invalid_forms(ref: str) -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [
        {
            "type": "git",
            "url": "https://github.com/example/direct.git",
            "ref": ref,
        }
    ]
    config = validate_final_config_structure(document)

    assert "custom_node.invalid_git_ref" in _codes(config)


def test_diagnostic_error_requires_stable_diagnostics_and_positive_exit_code() -> None:
    diagnostic = Diagnostic(("python", "version"), "python.invalid", "fix it")
    error = DiagnosticError((diagnostic,), exit_code=3)

    assert error.diagnostics == (diagnostic,)
    assert error.exit_code == 3

    with pytest.raises(ValueError, match="at least one"):
        DiagnosticError(())
    with pytest.raises(ValueError, match="positive"):
        DiagnosticError((diagnostic,), exit_code=0)
