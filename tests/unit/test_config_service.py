"""Active final configuration service contracts."""

from pathlib import Path

import pytest

from comfyui_docker_helper.config.service import (
    ConfigurationServiceError,
    load_validate_config,
    load_validate_config_result,
)


def _config() -> str:
    return """
[compute_platform]
type = "cuda"
[compute_platform.cuda]
version = "13.0.3"
[pytorch]
version = "2.12.1"
extra_packages = ["torchvision==0.27.1"]
[comfyui]
version = "0.11.0"
install_cli = true
install_manager = false
[build]
platforms = ["linux/amd64"]
"""


# Public loading validates locally and returns the typed configuration boundary.
def test_public_service_returns_validated_config_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config())

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("offline validation contacted an external boundary")

    monkeypatch.setattr("httpx.Client.get", forbidden)
    result = load_validate_config_result(path)

    assert result.config.python.version == "3.13.14"
    assert result.config.build.platforms == ["linux/amd64"]
    assert result.warnings == ()


# Layered files merge before validation so diagnostics observe effective input.
def test_layered_documents_merge_before_final_validation(tmp_path: Path) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(_config())
    override.write_text('[system]\nworkspace = "/data"\n')

    config = load_validate_config([base, override])

    assert config.system.workspace == "/data"
    assert config.pytorch.version == "2.12.1"


# Active isolated-tool requirements survive the public service boundary.
def test_public_service_accepts_active_uv_tools(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config() + '\n[python]\nuv_tools = ["ruff>=0.15,<0.16"]\n')

    assert load_validate_config(config).python.uv_tools == ["ruff>=0.15,<0.16"]


# Stable diagnostic ordering lets CLI adapters report all authored failures once.
def test_structural_domain_and_semantic_diagnostics_keep_stable_order(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        .replace('version = "13.0.3"', 'version = "bad"', 1)
        .replace(
            'platforms = ["linux/amd64"]',
            'platforms = ["linux/amd64", "linux/amd64"]',
        )
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config(config)

    assert [item.code for item in raised.value.diagnostics] == [
        "compute_platform.invalid_cuda_version",
        "build.duplicate_platform",
    ]


# File and TOML admission failures remain concise structured diagnostics.
def test_invalid_toml_and_missing_file_use_short_diagnostics(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[broken\n")
    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config(invalid)
    assert raised.value.diagnostics[0].code == "toml.invalid_document"

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config(tmp_path / "missing.toml")
    assert raised.value.diagnostics[0].code == "config.file_not_found"
