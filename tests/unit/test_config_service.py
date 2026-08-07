"""Final configuration service contracts."""

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

    assert result.config.build.platforms == ["linux/amd64"]
    assert result.warnings == ()


# The public shutdown budget accepts finite positive values and the exact
# disable sentinel while rejecting values that cannot drive the runtime owner.
@pytest.mark.parametrize(("value", "expected"), [("55.5", 55.5), ("-1", -1)])
def test_public_service_accepts_shutdown_timeout_contract(
    tmp_path: Path,
    value: str,
    expected: float,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config() + f"\n[cdh]\nshutdown_timeout = {value}\n")

    assert load_validate_config(path).cdh.shutdown_timeout == expected


@pytest.mark.parametrize("value", ["0", "-2", "nan", "inf", '"8"', "true"])
def test_public_service_rejects_invalid_shutdown_timeout(
    tmp_path: Path,
    value: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config() + f"\n[cdh]\nshutdown_timeout = {value}\n")

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config(path)

    assert [item.path for item in raised.value.diagnostics] == [
        ("cdh", "shutdown_timeout")
    ]


# Layered files merge before validation so diagnostics observe effective input.
def test_layered_documents_merge_before_final_validation(tmp_path: Path) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(_config())
    override.write_text('[system]\nworkspace = "/data"\n')

    config = load_validate_config([base, override])

    assert config.system.workspace == "/data"
    assert config.pytorch.version == "2.12.1"


def test_secret_and_git_credential_layers_compose_by_logical_keys(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    github = tmp_path / "github.toml"
    gitlab = tmp_path / "gitlab.toml"
    base.write_text(_config())
    github.write_text(
        """
[secrets.github_acme]
env = "CDH_GITHUB_ACME_TOKEN"

[[cdh.git.credentials]]
match = "https://github.com/acme/"
username = "x-access-token"
password = { secret = "github_acme" }
"""
    )
    gitlab.write_text(
        """
[secrets.gitlab_team]
file = "/run/secrets/gitlab-team"

[[cdh.git.credentials]]
match = "https://gitlab.example.com/team/"
username = "oauth2"
password = { secret = "gitlab_team" }
"""
    )

    config = load_validate_config([base, github, gitlab])

    assert tuple(config.secrets) == ("github_acme", "gitlab_team")
    assert [route.match for route in config.cdh.git.credentials] == [
        "https://github.com/acme/",
        "https://gitlab.example.com/team/",
    ]


def test_secret_and_git_credential_exact_overrides_are_atomic_and_stable(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(
        _config()
        + """

[secrets.shared]
env = "BASE_TOKEN"

[secrets.gitlab]
env = "GITLAB_TOKEN"

[[cdh.git.credentials]]
match = "https://github.com/acme/"
username = "base-user"
password = { secret = "shared" }

[[cdh.git.credentials]]
match = "https://gitlab.example.com/team/"
username = "oauth2"
password = { secret = "gitlab" }
"""
    )
    override.write_text(
        """
[secrets.shared]
file = "tokens/github"

[[cdh.git.credentials]]
match = "https://github.com/acme/"
username = "x-access-token"
password = { secret = "shared" }
"""
    )

    result = load_validate_config_result([base, override])

    assert result.raw_document["secrets"]["shared"] == {"file": "tokens/github"}
    routes = result.config.cdh.git.credentials
    assert [(route.match, route.username) for route in routes] == [
        ("https://github.com/acme/", "x-access-token"),
        ("https://gitlab.example.com/team/", "oauth2"),
    ]


def test_empty_git_credentials_layer_resets_all_inherited_routes(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    reset = tmp_path / "reset.toml"
    base.write_text(
        _config()
        + """

[secrets.github]
env = "GITHUB_TOKEN"

[[cdh.git.credentials]]
match = "https://github.com/acme/"
username = "x-access-token"
password = { secret = "github" }
"""
    )
    reset.write_text("[cdh.git]\ncredentials = []\n")

    result = load_validate_config_result([base, reset])

    assert result.config.cdh.git.credentials == []
    assert tuple(result.config.secrets) == ("github",)


def test_git_credentials_can_accumulate_again_after_a_reset(tmp_path: Path) -> None:
    base = tmp_path / "base.toml"
    reset = tmp_path / "reset.toml"
    later = tmp_path / "later.toml"
    base.write_text(
        _config()
        + """

[secrets.shared]
env = "PRIVATE_TOKEN"

[[cdh.git.credentials]]
match = "https://github.com/acme/"
username = "x-access-token"
password = { secret = "shared" }
"""
    )
    reset.write_text("[cdh.git]\ncredentials = []\n")
    later.write_text(
        """
[[cdh.git.credentials]]
match = "https://gitlab.example.com/team/"
username = "oauth2"
password = { secret = "shared" }
"""
    )

    result = load_validate_config_result([base, reset, later])

    assert [route.match for route in result.config.cdh.git.credentials] == [
        "https://gitlab.example.com/team/"
    ]


def test_http_git_credentials_are_returned_as_non_blocking_warnings(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + """

[secrets.private_git]
file = "missing-token-file"

[[cdh.git.credentials]]
match = "http://git.example.com/team/"
username = "token-user"
password = { secret = "private_git" }
"""
    )

    result = load_validate_config_result(config)

    assert [
        (item.path, item.code, item.severity.value) for item in result.warnings
    ] == [
        (
            ("cdh", "git", "credentials", 0, "match"),
            "git_credential.insecure_http",
            "warning",
        )
    ]


def test_secret_file_base_uses_real_first_config_parent_without_source_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_dir = tmp_path / "real"
    link_dir = tmp_path / "links"
    overlay_dir = tmp_path / "overlay"
    real_dir.mkdir()
    link_dir.mkdir()
    overlay_dir.mkdir()
    real_config = real_dir / "base.toml"
    real_config.write_text(
        _config()
        + """

[secrets.missing_env]
env = "CDH_TEST_MISSING_TOKEN"

[secrets.missing_file]
file = "never-read-token"
"""
    )
    linked_config = link_dir / "base.toml"
    linked_config.symlink_to(real_config)
    overlay = overlay_dir / "extra.toml"
    overlay.write_text('[system]\nworkspace = "/data"\n')
    original_open = Path.open

    def guarded_open(path: Path, *args: object, **kwargs: object):
        if path.name == "never-read-token":
            raise AssertionError("configuration validation read a Secret source")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    result = load_validate_config_result([linked_config, overlay])

    assert result.secret_file_base == real_dir.resolve()
    assert result.config.secrets["missing_file"].file == "never-read-token"


@pytest.mark.parametrize("package", ["bash", "tini"])
def test_layered_documents_report_default_os_package_collision(
    tmp_path: Path,
    package: str,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(_config())
    override.write_text(f'[system]\nextra_packages = ["{package}"]\n')

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config([base, override])

    assert [(item.path, item.code) for item in raised.value.diagnostics] == [
        (("system", "extra_packages", 0), "system.duplicate_apt_package")
    ]


# Isolated-tool requirements survive the public service boundary.
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
