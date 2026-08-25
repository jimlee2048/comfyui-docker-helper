"""Public-configuration structure contracts."""

import pytest

from comfyui_docker_helper.config.authored.validation.result import FinalConfigError
from comfyui_docker_helper.config.authored.validation.structure import (
    validate_final_config_structure,
)
from comfyui_docker_helper.config.diagnostics import (
    DiagnosticSeverity,
)
from tests.final_config_support import (
    _credential_document,
    _document,
)


# Final configuration admits strict public types and enforces cross-field ownership.
def test_final_structure_uses_exact_baseline_defaults() -> None:
    config = validate_final_config_structure(_document())

    assert config.python.version == "3.13.14"
    assert config.python.uv_version == "latest"
    assert config.build.platforms == ["linux/amd64"]
    assert config.compute_platform.cuda.image_flavor == "cudnn-devel"
    assert config.compute_platform.cuda.image_distro == "ubuntu24.04"
    assert config.comfyui.install_cli is True


def test_secret_sources_and_git_credentials_use_typed_complete_values() -> None:
    config = validate_final_config_structure(_credential_document())

    assert config.secrets["private_git"].env == "CDH_PRIVATE_GIT_TOKEN"
    route = config.cdh.git.credentials[0]
    assert route.password.secret == "private_git"

    document = _credential_document()
    document["cdh"]["git"]["credentials"][0]["password"] = "plaintext-token"
    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)
    assert any(
        (item.path, item.code, item.severity)
        == (
            ("cdh", "git", "credentials", 0, "password"),
            "schema.model_type",
            DiagnosticSeverity.ERROR,
        )
        for item in raised.value.diagnostics
    )


def test_downloader_credentials_use_typed_bearer_secret_references() -> None:
    document = _document()
    document["secrets"] = {"hf_read": {"env": "HF_TOKEN"}}
    document["cdh"] = {
        "downloader": {
            "credentials": [
                {
                    "match": "https://huggingface.co/acme/private/",
                    "type": "bearer",
                    "token": {"secret": "hf_read"},
                }
            ]
        }
    }
    config = validate_final_config_structure(document)

    route = config.cdh.downloader.credentials[0]
    assert route.type == "bearer"
    assert route.token.secret == "hf_read"

    document["cdh"]["downloader"]["credentials"][0]["token"] = "inline-token"
    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)
    assert any(
        (item.path, item.code, item.severity)
        == (
            ("cdh", "downloader", "credentials", 0, "token"),
            "schema.model_type",
            DiagnosticSeverity.ERROR,
        )
        for item in raised.value.diagnostics
    )


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

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("compute_platform", "cuda", field),
            "schema.literal_error",
            DiagnosticSeverity.ERROR,
        )
        for item in raised.value.diagnostics
    )


@pytest.mark.parametrize("value", [0, 1, "true", "false"])
def test_install_cli_is_a_strict_boolean(value: object) -> None:
    document = _document()
    document["comfyui"]["install_cli"] = value

    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)

    assert any(
        (item.path, item.code, item.severity)
        == (
            ("comfyui", "install_cli"),
            "schema.bool_type",
            DiagnosticSeverity.ERROR,
        )
        for item in raised.value.diagnostics
    )


# Structural and scalar domains reject coercion, ambiguity, and unsupported values.
def test_strict_structure_forbids_unknown_fields_and_coercion() -> None:
    document = _document()
    document["build"] = {"platforms": ["linux/amd64"], "unknown": True}
    document["comfyui"]["port"] = "8188"

    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)

    assert {
        (item.path, item.code, item.severity) for item in raised.value.diagnostics
    } >= {
        (
            ("build", "unknown"),
            "schema.extra_forbidden",
            DiagnosticSeverity.ERROR,
        ),
        (("comfyui", "port"), "schema.int_type", DiagnosticSeverity.ERROR),
    }


def test_platforms_are_nonempty_and_typed() -> None:
    document = _document()
    document["build"] = {"platforms": []}
    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)
    assert any(
        (item.path, item.code, item.severity)
        == (
            ("build", "platforms"),
            "schema.too_short",
            DiagnosticSeverity.ERROR,
        )
        for item in raised.value.diagnostics
    )

    document["build"] = {"platforms": ["linux/arm64"]}
    with pytest.raises(FinalConfigError) as raised:
        validate_final_config_structure(document)
    assert any(
        (item.path, item.code, item.severity)
        == (
            ("build", "platforms", 0),
            "schema.literal_error",
            DiagnosticSeverity.ERROR,
        )
        for item in raised.value.diagnostics
    )
