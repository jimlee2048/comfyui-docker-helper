"""Public-configuration domain contracts."""

from pathlib import Path

import pytest
from tests.final_config_support import (
    _credential_document,
    _document,
)

from comfyui_docker_helper.config.authored.validation.domains import (
    validate_final_config_domains,
)
from comfyui_docker_helper.config.authored.validation.semantics import (
    validate_final_config_semantics,
)
from comfyui_docker_helper.config.authored.validation.structure import (
    validate_final_config_structure,
)
from comfyui_docker_helper.config.diagnostics import DiagnosticSeverity

_PRIVATE_SECRET_PATH = ("secrets", "private_git")
_CREDENTIAL_PATH = ("cdh", "git", "credentials", 0)
_CREDENTIAL_SECRET_PATH = (*_CREDENTIAL_PATH, "password", "secret")
_VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "first@example"
)


def test_downloader_credential_routes_report_route_and_reference_diagnostics() -> None:
    document = _document()
    document["secrets"] = {"model_read": {"env": "MODEL_TOKEN"}}
    document["cdh"] = {
        "downloader": {
            "credentials": [
                {
                    "match": "http://EXAMPLE.com:80/models/",
                    "type": "bearer",
                    "token": {"secret": "model_read"},
                },
                {
                    "match": "http://example.com/models",
                    "type": "bearer",
                    "token": {"secret": "missing"},
                },
            ]
        },
    }
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    semantics = validate_final_config_semantics(config, domains)

    assert {(item.path, item.code, item.severity) for item in domains.diagnostics} == {
        (
            ("cdh", "downloader", "credentials", 0, "match"),
            "downloader_credential.insecure_http",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("cdh", "downloader", "credentials", 1, "match"),
            "downloader_credential.insecure_http",
            DiagnosticSeverity.WARNING,
        ),
    }
    assert {(item.path, item.code, item.severity) for item in semantics} == {
        (
            ("cdh", "downloader", "credentials", 1, "match"),
            "downloader_credential.duplicate_match",
            DiagnosticSeverity.ERROR,
        ),
        (
            (
                "cdh",
                "downloader",
                "credentials",
                1,
                "token",
                "secret",
            ),
            "secret.unknown_reference",
            DiagnosticSeverity.ERROR,
        ),
    }


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

    assert validate_final_config_domains(config).diagnostics == ()


@pytest.mark.parametrize(
    "locator",
    [
        pytest.param("", id="empty"),
        pytest.param("token\x00file", id="embedded-nul"),
        pytest.param("token\nfile", id="embedded-newline"),
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

    diagnostics = validate_final_config_domains(config).diagnostics

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
    domains = validate_final_config_domains(config)
    semantics = validate_final_config_semantics(config, domains)

    expected_domain = tuple(
        (path, code) for path, code in expected if code != "secret.unknown_reference"
    )
    expected_semantic = tuple(
        (path, code) for path, code in expected if code == "secret.unknown_reference"
    )
    assert tuple(
        (item.path, item.code, item.severity) for item in domains.diagnostics
    ) == tuple((path, code, DiagnosticSeverity.ERROR) for path, code in expected_domain)
    assert tuple((item.path, item.code, item.severity) for item in semantics) == tuple(
        (path, code, DiagnosticSeverity.ERROR) for path, code in expected_semantic
    )
    if mutation == "password-userinfo":
        assert all(
            "synthetic-marker" not in item.message
            for item in (*domains.diagnostics, *semantics)
        )


def test_git_credential_username_uses_the_protocol_utf8_byte_limit() -> None:
    document = _credential_document()
    route = document["cdh"]["git"]["credentials"][0]
    route["username"] = "é" * 32_762 + "a"
    maximum = validate_final_config_structure(document)

    maximum_domains = validate_final_config_domains(maximum)
    assert all(
        item.code != "git_credential.invalid_username"
        for item in maximum_domains.diagnostics
    )
    route["username"] += "a"
    oversized = validate_final_config_structure(document)
    diagnostics = validate_final_config_domains(oversized).diagnostics

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("cdh", "git", "credentials", 0, "username"),
            "git_credential.invalid_username",
            DiagnosticSeverity.ERROR,
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

    diagnostics = validate_final_config_domains(config).diagnostics

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("comfyui", "custom_nodes", 1, "url"),
            "custom_node.password_userinfo_forbidden",
            DiagnosticSeverity.ERROR,
        )
    ]
    assert "synthetic-marker" not in diagnostics[0].message


def test_uv_tools_are_active_strict_isolated_requirements() -> None:
    document = _document()
    document["python"] = {"uv_tools": ["Ruff==0.15.18", "mypy[dmypy]>=1,<2"]}

    config = validate_final_config_structure(document)

    assert config.python.uv_tools == ["Ruff==0.15.18", "mypy[dmypy]>=1,<2"]
    assert validate_final_config_domains(config).diagnostics == ()


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

    diagnostics = validate_final_config_domains(config).diagnostics

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("system", "env", name),
            "system.managed_env_override",
            DiagnosticSeverity.ERROR,
        )
    ]


def test_system_env_preserves_non_package_runtime_values() -> None:
    document = _document()
    document["system"] = {
        "env": {
            "APP_PROFILE": "production",
            "PIPER_MODE": "fast",
            "TZ": "Asia/Shanghai",
            "UVICORN_WORKERS": "2",
        }
    }
    config = validate_final_config_structure(document)

    assert validate_final_config_domains(config).diagnostics == ()
    assert config.system.env == {
        "APP_PROFILE": "production",
        "PIPER_MODE": "fast",
        "TZ": "Asia/Shanghai",
        "UVICORN_WORKERS": "2",
    }


@pytest.mark.parametrize("package", ["Bash", "x"])
def test_system_extra_package_rejects_noncanonical_debian_name(package: str) -> None:
    document = _document()
    document["system"] = {"extra_packages": [package]}
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("system", "extra_packages", 0),
            "system.invalid_apt_package",
            DiagnosticSeverity.ERROR,
        )
    ]


def test_system_extra_package_accepts_lowercase_debian_punctuation() -> None:
    document = _document()
    document["system"] = {"extra_packages": ["libfoo+bar.1-dev"]}
    config = validate_final_config_structure(document)

    assert validate_final_config_domains(config).diagnostics == ()


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


@pytest.mark.parametrize("version", ["3.13", "3.13.14rc1", "latest", " 3.13.14"])
def test_python_requires_an_exact_stable_patch(version: str) -> None:
    document = _document()
    document["python"] = {"version": version}
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("python", "version"),
            "python.exact_patch_required",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


@pytest.mark.parametrize("version", ["3.11.9", "3.15.0"])
def test_python_rejects_versions_outside_package_support(version: str) -> None:
    document = _document()
    document["python"] = {"version": version}
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("python", "version"),
            "python.unsupported_version",
            DiagnosticSeverity.ERROR,
        )
    ]


def test_python_accepts_unlisted_patch_inside_package_support() -> None:
    document = _document()
    document["python"] = {"version": "3.13.15"}
    config = validate_final_config_structure(document)

    assert validate_final_config_domains(config).diagnostics == ()


@pytest.mark.parametrize("selector", ["0.11.28", "latest"])
def test_uv_release_selector_accepts_exact_or_rolling_authority(selector: str) -> None:
    document = _document()
    document["python"] = {"uv_version": selector}
    config = validate_final_config_structure(document)

    assert validate_final_config_domains(config).diagnostics == ()


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

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("python", "uv_version"),
            "python.invalid_uv_version",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


@pytest.mark.parametrize("version", ["2.12", "2.12.1rc1", "latest", "v2.12.1"])
def test_pytorch_requires_an_exact_stable_public_version(version: str) -> None:
    document = _document()
    document["pytorch"]["version"] = version
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("pytorch", "version"),
            "pytorch.exact_stable_version_required",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


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

    assert any(
        (item.path, item.code, item.severity)
        == ((section, field), code, DiagnosticSeverity.ERROR)
        for item in domains.diagnostics
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param(" ", id="single-space"),
        pytest.param("1M\nprobe", id="embedded-newline"),
        "-1M",
    ],
)
def test_aria2_min_split_size_rejects_ambiguous_argv_values(value: str) -> None:
    document = _document()
    document["cdh"] = {"downloader": {"aria2": {"min_split_size": value}}}
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("cdh", "downloader", "aria2", "min_split_size"),
            "cdh.downloader.invalid_aria2_min_split_size",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


# ComfyUI and Registry selectors preserve stable supported release identities.
@pytest.mark.parametrize("version", ["0.3.60", "v0.3.60", "<0.11.0", "==0.3.60"])
def test_comfyui_rejects_selectors_definitely_below_floor(version: str) -> None:
    document = _document()
    document["comfyui"]["version"] = version
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("comfyui", "version"),
            "comfyui.version_below_floor",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


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

    diagnostics = validate_final_config_domains(config).diagnostics

    assert not any(item.severity == DiagnosticSeverity.ERROR for item in diagnostics)


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

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (("comfyui", "version"), code, DiagnosticSeverity.ERROR)
        for item in diagnostics
    )


def test_comfyui_selector_satisfiability_uses_discrete_formal_releases() -> None:
    document = _document()
    document["comfyui"]["version"] = ">=0.12.0,<0.12.1"
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert not any(item.severity == DiagnosticSeverity.ERROR for item in diagnostics)


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

    assert validate_final_config_domains(config).diagnostics == ()


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

    diagnostics = validate_final_config_domains(config).diagnostics

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("build", "tags", 0),
            "build.release_unavailable",
            DiagnosticSeverity.ERROR,
        )
    ]


def test_invalid_comfyui_selector_does_not_cascade_release_diagnostic() -> None:
    document = _document()
    document["comfyui"]["version"] = "not a selector"
    document["build"] = {"tags": ["example/image:v${{ comfyui.release }}"]}
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("comfyui", "version"),
            "comfyui.invalid_version",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )
    assert not any(item.code == "build.release_unavailable" for item in diagnostics)


def test_exact_registry_prerelease_remains_a_valid_published_selector() -> None:
    document = _document()
    document["comfyui"]["install_manager"] = True
    document["comfyui"]["custom_nodes"] = [
        {"type": "registry", "id": "example", "version": "1.0.0-rc.1"}
    ]
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert not any(item.severity == DiagnosticSeverity.ERROR for item in diagnostics)


@pytest.mark.parametrize("node_id", ["invalid/name", "invalid!name"])
def test_registry_id_requires_valid_project_name(node_id: str) -> None:
    document = _document()
    document["comfyui"]["install_manager"] = True
    document["comfyui"]["custom_nodes"] = [
        {"type": "registry", "id": node_id, "version": "1.0.0"}
    ]
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("comfyui", "custom_nodes", 0, "id"),
            "custom_node.invalid_registry_id",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


def test_registry_selector_ranges_reject_prerelease_operands() -> None:
    document = _document()
    document["comfyui"]["install_manager"] = True
    document["comfyui"]["custom_nodes"] = [
        {"type": "registry", "id": "example", "version": ">=1.0.0-rc.1,<2"}
    ]
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("comfyui", "custom_nodes", 0, "version"),
            "custom_node.invalid_registry_version",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


@pytest.mark.parametrize(
    ("requirement", "code"),
    [
        (" demo==1", "python.invalid_requirement"),
        ("demo @ file:///tmp/demo.whl", "python.unsupported_direct_reference"),
        (
            "demo @ https://user@example.com/demo.whl",
            "python.unsupported_direct_reference",
        ),
    ],
)
def test_python_requirement_domain_rejects_invalid_or_unsupported_inputs(
    requirement: str,
    code: str,
) -> None:
    document = _document()
    document["python"] = {"extra_packages": [requirement]}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert domains.package_requirements == ()
    assert [(item.path, item.code, item.severity) for item in domains.diagnostics] == [
        (
            ("python", "extra_packages", 0),
            code,
            DiagnosticSeverity.ERROR,
        )
    ]


def test_python_requirement_domain_does_not_pre_solve_standard_selector() -> None:
    document = _document()
    document["python"] = {"extra_packages": ["demo==1,==2"]}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert domains.diagnostics == ()
    assert domains.package_requirements[0].specifier == "==1,==2"


@pytest.mark.parametrize(
    ("group", "field", "marker", "active"),
    [
        ("python", "extra_packages", 'python_version == "3.13"', True),
        ("python", "extra_packages", 'python_version < "3.13"', False),
        ("python", "uv_tools", 'platform_system == "Linux"', True),
        ("python", "uv_tools", 'platform_system == "Windows"', False),
        ("pytorch", "extra_packages", 'platform_machine == "x86_64"', True),
        ("pytorch", "extra_packages", 'platform_machine == "aarch64"', False),
    ],
)
def test_requirement_fields_project_representative_target_markers(
    group: str,
    field: str,
    marker: str,
    active: bool,
) -> None:
    document = _document()
    document.setdefault(group, {})[field] = [f"demo; {marker}"]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert domains.diagnostics == ()
    assert len(domains.authored_package_requirements) == 1
    assert bool(domains.package_requirements) is active
    if active:
        assert domains.package_requirements == domains.authored_package_requirements


def test_requirement_marker_uses_empty_unavailable_kernel_value() -> None:
    document = _document()
    document["python"] = {
        "extra_packages": ['demo; platform_release == ""'],
    }
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert domains.diagnostics == ()
    assert domains.package_requirements == domains.authored_package_requirements


def test_requirement_domain_rejects_undefined_containing_marker_context() -> None:
    document = _document()
    document["python"] = {
        "uv_tools": ['demo; "gpu" in dependency_groups'],
    }
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert [(item.path, item.code, item.severity) for item in domains.diagnostics] == [
        (
            ("python", "uv_tools", 0),
            "python.unsupported_marker_context",
            DiagnosticSeverity.ERROR,
        )
    ]
    assert len(domains.authored_package_requirements) == 1
    assert domains.package_requirements == ()


def test_invalid_target_keeps_only_unmarked_requirement_active() -> None:
    document = _document()
    document["python"] = {
        "version": "latest",
        "extra_packages": [
            "plain",
            'marked; python_version >= "3.12"',
        ],
    }
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert [item.name for item in domains.authored_package_requirements] == [
        "plain",
        "marked",
    ]
    assert [item.name for item in domains.package_requirements] == ["plain"]


def test_undefined_marker_comparison_is_a_domain_diagnostic() -> None:
    document = _document()
    document["python"] = {"extra_packages": ['demo; os_name ~= "posix"']}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert [(item.path, item.code, item.severity) for item in domains.diagnostics] == [
        (
            ("python", "extra_packages", 0),
            "python.invalid_environment_marker",
            DiagnosticSeverity.ERROR,
        )
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


def test_file_directory_normalization_supports_the_comfyui_root() -> None:
    document = _document()
    document["files"] = [
        {
            "type": "http",
            "url": "https://example.com/root",
            "target_dir": "./",
            "filename": "root.bin",
        }
    ]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert domains.diagnostics == ()
    assert [item.directory.as_posix() for item in domains.files] == ["."]
    assert [item.relative_target for item in domains.files] == ["root.bin"]
    assert config.files[0].target_dir == "./"


def test_file_target_rejects_only_the_exact_internal_staging_leaf() -> None:
    document = _document()
    document["files"] = [
        {
            "type": "http",
            "url": "https://example.com/reserved",
            "target_dir": "models",
            "filename": ".cdh-staging",
        },
        {
            "type": "local",
            "path": "ordinary.bin",
            "target_dir": ".cdh-staging",
            "filename": ".cdh-staging.part",
        },
    ]
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert [
        (item.path, item.code, item.severity)
        for item in diagnostics
        if item.code.startswith("file.")
    ] == [
        (
            ("files", 0, "filename"),
            "file.invalid_filename",
            DiagnosticSeverity.ERROR,
        )
    ]


@pytest.mark.parametrize(
    "url",
    ["", "local/path", "https://", "file:///tmp/repo", "-ssh://host/repo"],
)
def test_git_source_url_requires_a_supported_remote_form(url: str) -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [{"type": "git", "url": url}]
    config = validate_final_config_structure(document)

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("comfyui", "custom_nodes", 0, "url"),
            "custom_node.invalid_git_url",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


def test_invalid_git_url_does_not_suppress_independent_field_diagnostics() -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [
        {"type": "git", "url": "bad", "ref": "-bad", "target_dir": ".."}
    ]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert [(item.path, item.code, item.severity) for item in domains.diagnostics] == [
        (
            ("comfyui", "custom_nodes", 0, "url"),
            "custom_node.invalid_git_url",
            DiagnosticSeverity.ERROR,
        ),
        (
            ("comfyui", "custom_nodes", 0, "ref"),
            "custom_node.invalid_git_ref",
            DiagnosticSeverity.ERROR,
        ),
        (
            ("comfyui", "custom_nodes", 0, "target_dir"),
            "custom_node.invalid_git_target_dir",
            DiagnosticSeverity.ERROR,
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

    diagnostics = validate_final_config_domains(config).diagnostics

    assert not any(item.severity == DiagnosticSeverity.ERROR for item in diagnostics)


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

    diagnostics = validate_final_config_domains(
        config, build_hooks_dir=tmp_path
    ).diagnostics

    assert [
        (item.path, item.code, item.severity)
        for item in diagnostics
        if item.code == "hook.source_not_regular"
    ] == [
        (
            ("comfyui", "custom_nodes", 0, "pre_install_hooks", 1),
            "hook.source_not_regular",
            DiagnosticSeverity.ERROR,
        )
    ]
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

    diagnostics = validate_final_config_domains(config).diagnostics

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("comfyui", "custom_nodes", 0, "ref"),
            "custom_node.invalid_git_ref",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )
