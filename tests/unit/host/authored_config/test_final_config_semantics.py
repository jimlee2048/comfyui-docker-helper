"""Public-configuration semantic contracts."""

import pytest

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
from tests.final_config_support import (
    _credential_document,
    _diagnostics,
    _document,
)


def test_authenticated_download_requires_httpx_with_actionable_hint() -> None:
    document = _document()
    document["secrets"] = {"model_read": {"env": "MODEL_TOKEN"}}
    document["cdh"] = {
        "default_downloader": "aria2",
        "downloader": {
            "credentials": [
                {
                    "match": "https://example.com/models/",
                    "type": "bearer",
                    "token": {"secret": "model_read"},
                }
            ]
        },
    }
    document["files"] = [
        {
            "type": "http",
            "source": "https://example.com/models/model.bin?download=true",
            "target": "models/checkpoints/model.bin",
        }
    ]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    diagnostics = validate_final_config_semantics(config, domains)
    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("files", 0, "downloader"),
            "file.authenticated_downloader_requires_httpx",
            DiagnosticSeverity.ERROR,
        )
    ]
    diagnostic = next(
        item
        for item in diagnostics
        if item.code == "file.authenticated_downloader_requires_httpx"
    )
    assert diagnostic.hint is not None and "httpx" in diagnostic.hint

    document["files"][0]["downloader"] = "httpx"
    accepted = validate_final_config_structure(document)
    accepted_domains = validate_final_config_domains(accepted)
    accepted_diagnostics = validate_final_config_semantics(accepted, accepted_domains)
    assert not any(
        item.severity == DiagnosticSeverity.ERROR for item in accepted_diagnostics
    )


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

    domains = validate_final_config_domains(config)
    diagnostics = validate_final_config_semantics(config, domains)

    assert [(item.path, item.code, item.severity) for item in domains.diagnostics] == [
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
    ]
    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("cdh", "git", "credentials", 1, "match"),
            "git_credential.duplicate_match",
            DiagnosticSeverity.ERROR,
        ),
    ]


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


def test_duplicate_platforms_are_rejected_semantically() -> None:
    document = _document()
    document["build"] = {"platforms": ["linux/amd64", "linux/amd64"]}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    assert domains.diagnostics == ()
    diagnostics = validate_final_config_semantics(config, domains)

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("build", "platforms", 1),
            "build.duplicate_platform",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


@pytest.mark.parametrize(
    ("uv_tools", "expected"),
    [
        (
            ["ruff", "Ruff==0.15.18"],
            (
                ("python", "uv_tools", 1),
                "python.conflicting_package_requirement",
            ),
        ),
        (
            ["comfyui-docker-helper==0.5.0"],
            (("python", "uv_tools", 0), "python.duplicate_package_owner"),
        ),
        (
            ["Comfy_CLI>=1.7"],
            (("python", "uv_tools", 0), "python.duplicate_package_owner"),
        ),
    ],
)
def test_uv_tools_reject_duplicate_or_reserved_owners(
    uv_tools: list[str],
    expected: tuple[tuple[str | int, ...], str],
) -> None:
    document = _document()
    document["python"] = {"uv_tools": uv_tools}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    assert domains.diagnostics == ()
    diagnostics = validate_final_config_semantics(config, domains)

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            expected[0],
            expected[1],
            DiagnosticSeverity.ERROR,
        )
    ]


# Package-owner diagnostics identify the field that retains install authority.
@pytest.mark.parametrize("install_cli", [True, False])
def test_comfy_cli_generic_tool_owner_is_reserved_in_both_modes(
    install_cli: bool,
) -> None:
    document = _document()
    document["comfyui"]["install_cli"] = install_cli
    document["python"] = {"uv_tools": ["Comfy_CLI"]}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    assert domains.diagnostics == ()
    diagnostics = validate_final_config_semantics(config, domains)

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("python", "uv_tools", 0),
            "python.duplicate_package_owner",
            DiagnosticSeverity.ERROR,
        )
    ]


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

    domains = validate_final_config_domains(config)
    assert domains.diagnostics == ()
    diagnostics = validate_final_config_semantics(config, domains)

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            (group, "extra_packages", 0),
            "python.duplicate_package_owner",
            DiagnosticSeverity.ERROR,
        )
    ]


# Public OS-package diagnostics cover the effective default-plus-user set and
# enforce canonical lowercase Debian identities before planning.
@pytest.mark.parametrize("package", ["bash", "tini", "tzdata"])
def test_system_extra_package_warns_and_filters_default_overlap(package: str) -> None:
    document = _document()
    document["system"] = {"extra_packages": [package]}
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    assert domains.diagnostics == ()
    diagnostics = validate_final_config_semantics(config, domains)

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

    domains = validate_final_config_domains(config)
    assert domains.diagnostics == ()
    diagnostics = validate_final_config_semantics(config, domains)

    assert [(item.path, item.code, item.severity) for item in diagnostics] == [
        (
            ("system", "extra_packages", 1),
            "system.duplicate_apt_package",
            DiagnosticSeverity.ERROR,
        )
    ]


def test_registry_nodes_require_manager_but_direct_git_nodes_do_not() -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [
        {"type": "git", "url": "https://github.com/example/direct.git"}
    ]
    git_config = validate_final_config_structure(document)
    git_domains = validate_final_config_domains(git_config)
    git_diagnostics = validate_final_config_semantics(git_config, git_domains)
    assert not any(
        item.severity == DiagnosticSeverity.ERROR for item in git_diagnostics
    )

    document["comfyui"]["custom_nodes"] = [
        {"type": "registry", "id": "example-node", "version": "1.0.0"}
    ]
    registry_config = validate_final_config_structure(document)
    registry_domains = validate_final_config_domains(registry_config)
    diagnostics = validate_final_config_semantics(registry_config, registry_domains)
    assert any(
        (item.path, item.code, item.severity)
        == (
            ("comfyui", "custom_nodes", 0, "type"),
            "custom_node.manager_required",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


def test_registry_punctuation_variants_report_distribution_identity_collision() -> None:
    document = _document()
    document["comfyui"]["install_manager"] = True
    document["comfyui"]["custom_nodes"] = [
        {"type": "registry", "id": "Example_Node", "version": "1.0.0"},
        {"type": "registry", "id": "example.node", "version": "1.1.0"},
    ]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    diagnostics = validate_final_config_semantics(config, domains)

    assert config.comfyui.custom_nodes[0].id == "Example_Node"
    assert any(
        (item.path, item.code, item.severity)
        == (
            ("comfyui", "custom_nodes", 1, "id"),
            "custom_node.registry_distribution_identity_collision",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


# Target marker projection precedes ownership and protected-source enforcement.
def test_package_ownership_is_normalized_across_groups() -> None:
    document = _document()
    document["python"] = {"extra_packages": ["My_Package[cli]>=1,<2"]}
    document["pytorch"]["extra_packages"] = ["my-package==1.5", "torch==2.12.1"]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    assert "python.duplicate_package_owner" not in {
        item.code for item in domains.diagnostics
    }
    diagnostics = validate_final_config_semantics(config, domains)

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("pytorch", "extra_packages", 0),
            "python.conflicting_package_requirement",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )
    assert any(
        (item.path, item.code, item.severity)
        == (
            ("pytorch", "extra_packages", 1),
            "python.duplicate_package_owner",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


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

    domains = validate_final_config_domains(config)
    diagnostics = validate_final_config_semantics(config, domains)

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("python", "extra_packages", 0),
            "python.duplicate_package_owner",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


def test_pytorch_extras_reject_protected_direct_source() -> None:
    document = _document()
    document["pytorch"]["extra_packages"] = [
        "torchvision @ https://example.test/torchvision.whl"
    ]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    diagnostics = validate_final_config_semantics(config, domains)

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("pytorch", "extra_packages", 0),
            "pytorch.protected_requirement_conflict",
            DiagnosticSeverity.ERROR,
        )
        for item in diagnostics
    )


def test_pytorch_extras_accept_protected_index_requirement() -> None:
    document = _document()
    document["pytorch"]["extra_packages"] = ["torchvision>=0.27"]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    assert validate_final_config_semantics(config, domains) == ()


def test_package_ownership_is_scoped_to_isolated_environment() -> None:
    document = _document()
    document["python"] = {
        "extra_packages": ["ruff==0.15.18"],
        "uv_tools": ["Ruff==0.15.18"],
    }
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    assert validate_final_config_semantics(config, domains) == ()


def test_inactive_application_requirement_does_not_claim_package_ownership() -> None:
    document = _document()
    document["python"] = {"extra_packages": ['demo<2; python_version < "3.13"']}
    document["pytorch"]["extra_packages"] = ['Demo>=2; python_version >= "3.13"']
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)

    assert len(domains.authored_package_requirements) == 2
    assert [item.path for item in domains.package_requirements] == [
        ("pytorch", "extra_packages", 0)
    ]
    assert validate_final_config_semantics(config, domains) == ()


# File, Git, and hook inputs are unique, contained, and safe for their consumers.
def test_duplicate_file_targets_are_detected_after_path_normalization() -> None:
    document = _document()
    document["files"] = [
        {
            "type": "http",
            "source": "https://example.com/a",
            "target": "models/x/a.bin",
        },
        {
            "type": "local",
            "source": "model.bin",
            "target": "models//x/./a.bin",
        },
    ]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    diagnostics = validate_final_config_semantics(config, domains)

    assert [
        (item.path, item.code, item.severity)
        for item in diagnostics
        if item.code == "file.overlapping_target"
    ] == [
        (
            ("files", 1, "target"),
            "file.overlapping_target",
            DiagnosticSeverity.ERROR,
        )
    ]


def test_file_target_regions_reject_component_prefix_overlap() -> None:
    document = _document()
    document["files"] = [
        {"type": "local", "source": "models", "target": "models"},
        {
            "type": "http",
            "source": "https://example.com/a",
            "target": "models/checkpoints/a.bin",
        },
    ]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    diagnostics = validate_final_config_semantics(config, domains)

    assert [
        (item.path, item.code, item.severity)
        for item in diagnostics
        if item.code == "file.overlapping_target"
    ] == [
        (
            ("files", 1, "target"),
            "file.overlapping_target",
            DiagnosticSeverity.ERROR,
        )
    ]


def test_duplicate_effective_git_targets_are_rejected() -> None:
    document = _document()
    document["comfyui"]["custom_nodes"] = [
        {"type": "git", "url": "https://github.com/a/one.git", "target_dir": "same"},
        {"type": "git", "url": "https://github.com/b/two.git", "target_dir": "same"},
    ]
    config = validate_final_config_structure(document)

    domains = validate_final_config_domains(config)
    diagnostics = validate_final_config_semantics(config, domains)

    assert [
        (item.path, item.code, item.severity)
        for item in diagnostics
        if item.code == "custom_node.duplicate_git_target_dir"
    ] == [
        (
            ("comfyui", "custom_nodes", 1, "target_dir"),
            "custom_node.duplicate_git_target_dir",
            DiagnosticSeverity.ERROR,
        )
    ]
