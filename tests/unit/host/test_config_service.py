"""Final configuration service contracts."""

from pathlib import Path

import pytest

from comfyui_docker_helper.config.diagnostics import (
    DiagnosticComparison,
    SourceLocation,
)
from comfyui_docker_helper.config.service import (
    ConfigurationServiceError,
    load_validate_config,
    load_validate_config_result,
)

_VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "first@example"
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


# Layered service integration proves merge and provenance before final validation.
def test_layered_documents_merge_before_final_validation(tmp_path: Path) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(_config())
    override.write_text('[system]\nworkspace = "/data"\n')

    config = load_validate_config([base, override])

    assert config.system.workspace == "/data"
    assert config.pytorch.version == "2.12.1"


def test_layered_service_exposes_effective_leaf_origins(tmp_path: Path) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(_config())
    override.write_text('[system]\nworkspace = "/data"\n')

    result = load_validate_config_result([base, override])

    retained = result.origins.exact_location(("pytorch", "version"))
    replaced = result.origins.exact_location(("system", "workspace"))
    assert retained is not None
    assert replaced is not None
    assert retained.source.layer_ordinal == 0
    assert retained.source.label == str(base)
    assert replaced.source.layer_ordinal == 1
    assert replaced.source.label == str(override)


def test_same_physical_config_path_remains_two_distinct_source_layers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config())

    single = load_validate_config_result(path)
    repeated = load_validate_config_result([path, path])

    assert repeated.config == single.config
    assert repeated.raw_document == single.raw_document
    assert repeated.secret_file_base == single.secret_file_base
    location = repeated.origins.exact_location(("build", "platforms"))
    assert location is not None
    assert location.source.layer_ordinal == 1
    assert location.source.label == str(path)


def test_service_tracks_keyed_file_overlay_and_append_origins(tmp_path: Path) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(
        _config()
        + """

[[files]]
type = "http"
url = "https://example.com/base.bin"
target_dir = "models"
filename = "model.bin"
checksum = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"""
    )
    override.write_text(
        """
[[files]]
type = "http"
url = "https://example.com/later.bin"
target_dir = "models"
filename = "model.bin"

[[files]]
type = "http"
url = "https://example.com/new.bin"
target_dir = "models"
filename = "new.bin"
"""
    )

    result = load_validate_config_result([base, override])

    assert [(item.filename, item.url) for item in result.config.files] == [
        ("model.bin", "https://example.com/later.bin"),
        ("new.bin", "https://example.com/new.bin"),
    ]
    base_checksum = result.origins.exact_location(("files", 0, "checksum"))
    later_url = result.origins.exact_location(("files", 0, "url"))
    appended = result.origins.exact_location(("files", 1))
    assert base_checksum is not None and base_checksum.source.layer_ordinal == 0
    assert later_url is not None and later_url.source.layer_ordinal == 1
    assert later_url.path == ("files", 0, "url")
    assert appended is not None and appended.source.layer_ordinal == 1
    assert appended.path == ("files", 1)


def test_service_tracks_an_empty_keyed_sequence_reset(tmp_path: Path) -> None:
    base = tmp_path / "base.toml"
    reset = tmp_path / "reset.toml"
    base.write_text(
        _config()
        + """

[[files]]
type = "http"
url = "https://example.com/base.bin"
target_dir = "models"
filename = "model.bin"
"""
    )
    reset.write_text("files = []\n")

    result = load_validate_config_result([base, reset])

    assert result.config.files == []
    location = result.origins.exact_location(("files",))
    assert location is not None
    assert location.source.layer_ordinal == 1
    assert location.source.label == str(reset)


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


def test_git_credentials_reset_then_accumulate_in_a_later_layer(tmp_path: Path) -> None:
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
    assert tuple(result.config.secrets) == ("shared",)


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
def test_layered_default_os_package_overlap_warns_once_from_effective_source(
    tmp_path: Path,
    package: str,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(_config() + f'\n[system]\nextra_packages = ["{package}"]\n')
    override.write_text(f'[system]\nextra_packages = ["{package}"]\n')

    result = load_validate_config_result([base, override])

    assert [(item.path, item.code) for item in result.warnings] == [
        (
            ("system", "extra_packages", 0),
            "system.redundant_default_apt_package",
        )
    ]
    context = result.warnings[0].source_context
    assert isinstance(context, SourceLocation)
    assert context.source.label == str(override)
    assert result.domains.authored_apt_packages[0].value == package
    assert result.domains.apt_packages == ()


def test_host_ssh_duplicate_warning_uses_winning_list_source_without_key_value(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(
        _config() + f'\n[system.ssh]\npub_keys = ["{_VALID_SSH_KEY}", "not-a-key"]\n'
    )
    duplicate = _VALID_SSH_KEY.rsplit(" ", 1)[0] + " second@example"
    override.write_text(
        f'[system.ssh]\npub_keys = ["  {_VALID_SSH_KEY}  ", "{duplicate}"]\n'
    )

    result = load_validate_config_result([base, override])

    assert [(item.path, item.code) for item in result.warnings] == [
        (("system", "ssh", "pub_keys", 1), "ssh.redundant_public_key")
    ]
    warning = result.warnings[0]
    assert isinstance(warning.source_context, SourceLocation)
    assert warning.source_context.source.label == str(override)
    assert warning.source_context.path == ("system", "ssh", "pub_keys", 1)
    assert _VALID_SSH_KEY not in warning.message
    assert result.domains.ssh_public_keys == (_VALID_SSH_KEY,)


# Isolated-tool requirements survive the public service boundary.
def test_public_service_accepts_active_uv_tools(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config() + '\n[python]\nuv_tools = ["ruff>=0.15,<0.16"]\n')

    assert load_validate_config(config).python.uv_tools == ["ruff>=0.15,<0.16"]


def test_layered_package_fields_compose_in_stable_order(tmp_path: Path) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(
        _config().replace(
            '[pytorch]\nversion = "2.12.1"',
            '[pytorch]\nversion = "2.12.1"\nextra_packages = ["xformers==0.0.30"]',
        )
        + """

[system]
extra_packages = ["git-lfs"]

[python]
extra_packages = ["demo==1"]
uv_tools = ["ruff==0.15.0"]
"""
    )
    override.write_text(
        """
[system]
extra_packages = ["ffmpeg"]

[python]
extra_packages = ["other==2"]
uv_tools = ["mypy==1.15.0"]

[pytorch]
extra_packages = ["triton==3.3.0"]
"""
    )

    config = load_validate_config([base, override])

    assert config.system.extra_packages == ["git-lfs", "ffmpeg"]
    assert config.python.extra_packages == ["demo==1", "other==2"]
    assert config.python.uv_tools == ["ruff==0.15.0", "mypy==1.15.0"]
    assert config.pytorch.extra_packages == [
        "xformers==0.0.30",
        "triton==3.3.0",
    ]


def test_layered_canonical_requirement_dedup_retains_later_spelling(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(_config() + '\n[python]\nextra_packages = ["Demo[B,A]>=1,<2"]\n')
    override.write_text('[python]\nextra_packages = ["demo[a,b]<2,>=1", "other==2"]\n')

    result = load_validate_config_result([base, override])

    assert result.config.python.extra_packages == [
        "demo[a,b]<2,>=1",
        "other==2",
    ]
    location = result.origins.exact_location(("python", "extra_packages", 0))
    assert location is not None and location.source.layer_ordinal == 1


@pytest.mark.parametrize(
    ("base_requirement", "later_requirement", "canonical"),
    [
        ("Demo~=1.2", "demo ~= 1.2", "demo~=1.2"),
        ("Demo>=1,!=2", "demo!=2,>=1", "demo!=2,>=1"),
    ],
)
def test_layered_new_selector_forms_use_existing_canonical_dedup(
    tmp_path: Path,
    base_requirement: str,
    later_requirement: str,
    canonical: str,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(
        _config() + f'\n[python]\nextra_packages = ["{base_requirement}"]\n'
    )
    override.write_text(f'[python]\nextra_packages = ["{later_requirement}"]\n')

    result = load_validate_config_result([base, override])

    assert result.config.python.extra_packages == [later_requirement]
    assert result.domains.package_requirements[0].canonical_value == canonical


def test_compatible_and_expanded_range_remain_distinct_requirements(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(_config() + '\n[python]\nextra_packages = ["demo~=1.2"]\n')
    override.write_text('[python]\nextra_packages = ["Demo>=1.2,<2"]\n')

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config_result([base, override])

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "python.conflicting_package_requirement"
    assert isinstance(diagnostic.source_context, DiagnosticComparison)
    assert diagnostic.source_context.earlier.display_value == "demo~=1.2"
    assert diagnostic.source_context.later.display_value == "Demo>=1.2,<2"


def test_layered_requirement_conflict_reports_symmetric_sources(tmp_path: Path) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(_config() + '\n[python]\nextra_packages = ["demo>=1,<2"]\n')
    override.write_text('[python]\nextra_packages = ["Demo>=2,<3"]\n')

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config_result([base, override])

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "python.conflicting_package_requirement"
    assert diagnostic.hint == "Use one requirement for this package."
    assert isinstance(diagnostic.source_context, DiagnosticComparison)
    assert diagnostic.source_context.earlier.location.source.label == str(base)
    assert diagnostic.source_context.earlier.display_value == "demo>=1,<2"
    assert diagnostic.source_context.later.location.source.label == str(override)
    assert diagnostic.source_context.later.display_value == "Demo>=2,<3"


def test_layered_inactive_requirement_does_not_conflict_with_active_owner(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(
        _config()
        + "\n[python]\n"
        + "extra_packages = [\"demo<2; python_version < '3.13'\"]\n"
    )
    override.write_text(
        "[python]\n" + "extra_packages = [\"Demo>=2; python_version >= '3.13'\"]\n"
    )

    result = load_validate_config_result([base, override])

    assert result.config.python.extra_packages == [
        "demo<2; python_version < '3.13'",
        "Demo>=2; python_version >= '3.13'",
    ]
    assert len(result.domains.authored_package_requirements) == 2
    assert [item.path for item in result.domains.package_requirements] == [
        ("python", "extra_packages", 1)
    ]


def test_registry_case_variant_overlays_and_retains_later_spelling(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(
        _config().replace("install_manager = false", "install_manager = true")
        + """

[[comfyui.custom_nodes]]
type = "registry"
id = "Example_Node"
version = "1.0.0"
"""
    )
    override.write_text(
        """
[[comfyui.custom_nodes]]
type = "registry"
id = "example_node"
version = "2.0.0"
"""
    )

    result = load_validate_config_result([base, override])

    node = result.config.comfyui.custom_nodes[0]
    assert node.id == "example_node"
    assert node.version == "2.0.0"
    location = result.origins.exact_location(("comfyui", "custom_nodes", 0, "id"))
    assert location is not None and location.source.label == str(override)


def test_registry_punctuation_collision_reports_both_resources(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _config().replace("install_manager = false", "install_manager = true")
        + """

[[comfyui.custom_nodes]]
type = "registry"
id = "example_node"
version = "1.0.0"

[[comfyui.custom_nodes]]
type = "registry"
id = "example.node"
version = "2.0.0"
"""
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config_result(config)

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "custom_node.registry_distribution_identity_collision"
    assert isinstance(diagnostic.source_context, DiagnosticComparison)
    assert diagnostic.source_context.earlier.display_value == "example_node"
    assert diagnostic.source_context.later.display_value == "example.node"


def test_same_layer_registry_case_duplicate_is_a_resource_conflict(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _config().replace("install_manager = false", "install_manager = true")
        + """

[[comfyui.custom_nodes]]
type = "registry"
id = "Example_Node"
version = "1.0.0"

[[comfyui.custom_nodes]]
type = "registry"
id = "example_node"
version = "2.0.0"
"""
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config_result(config)

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "custom_node.duplicate_registry_id"
    assert isinstance(diagnostic.source_context, DiagnosticComparison)
    assert diagnostic.source_context.earlier.display_value == "Example_Node"
    assert diagnostic.source_context.later.display_value == "example_node"


def test_three_registry_distribution_collisions_compare_first_with_each_later(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _config().replace("install_manager = false", "install_manager = true")
        + """

[[comfyui.custom_nodes]]
type = "registry"
id = "example_node"
version = "1.0.0"

[[comfyui.custom_nodes]]
type = "registry"
id = "example.node"
version = "2.0.0"

[[comfyui.custom_nodes]]
type = "registry"
id = "example-node"
version = "3.0.0"
"""
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config_result(config)

    diagnostics = raised.value.diagnostics
    assert [diagnostic.code for diagnostic in diagnostics] == [
        "custom_node.registry_distribution_identity_collision",
        "custom_node.registry_distribution_identity_collision",
    ]
    contexts = [diagnostic.source_context for diagnostic in diagnostics]
    assert all(isinstance(context, DiagnosticComparison) for context in contexts)
    assert [
        (
            context.earlier.display_value,
            context.later.display_value,
        )
        for context in contexts
        if isinstance(context, DiagnosticComparison)
    ] == [
        ("example_node", "example.node"),
        ("example_node", "example-node"),
    ]


def test_canonical_credential_overlay_missing_field_uses_later_item_source(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(
        _config()
        + """

[secrets.base]
env = "BASE_TOKEN"

[[cdh.git.credentials]]
match = "https://GitHub.com:443/acme"
username = "base"
password = { secret = "base" }
"""
    )
    override.write_text(
        """
[[cdh.git.credentials]]
match = "https://github.com/acme/"
username = "later"
"""
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config_result([base, override])

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "schema.missing"
    assert diagnostic.path == ("cdh", "git", "credentials", 0, "password")
    assert diagnostic.source_context is not None
    assert not isinstance(diagnostic.source_context, DiagnosticComparison)
    assert diagnostic.source_context.source.label == str(override)
    assert diagnostic.source_context.path == ("cdh", "git", "credentials", 0)


def test_same_layer_canonical_credential_duplicate_omits_display_values(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + """

[secrets.private]
env = "PRIVATE_TOKEN"

[[cdh.git.credentials]]
match = "https://GitHub.com:443/acme"
username = "first"
password = { secret = "private" }

[[cdh.git.credentials]]
match = "https://github.com/acme/"
username = "second"
password = { secret = "private" }
"""
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config_result(config)

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "git_credential.duplicate_match"
    assert isinstance(diagnostic.source_context, DiagnosticComparison)
    assert diagnostic.source_context.earlier.display_value is None
    assert diagnostic.source_context.later.display_value is None


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
    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "toml.invalid_document"
    assert isinstance(diagnostic.source_context, SourceLocation)
    assert diagnostic.source_context.source.label == str(invalid)

    missing = tmp_path / "missing.toml"
    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_config(missing)
    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "config.file_not_found"
    assert isinstance(diagnostic.source_context, SourceLocation)
    assert diagnostic.source_context.source.label == str(missing)
