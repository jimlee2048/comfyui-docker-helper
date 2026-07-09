"""Tests for the reusable load, validate, and plan service."""

from collections.abc import Callable
from pathlib import Path

import pytest

from comfyui_docker_helper.config import (
    ConfigurationServiceError,
    DiagnosticSeverity,
    RenderPlan,
    load_validate_plan,
    load_validate_plan_result,
)

MINIMAL_CONFIG = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"

[comfyui]
version = "latest"
"""
TRUNCATED_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 truncated"


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    """Write a service input without coupling service tests to loader tests."""

    def write(document: str) -> Path:
        path = tmp_path / "config.toml"
        path.write_text(document, encoding="utf-8")
        return path

    return write


def _identities(error: ConfigurationServiceError) -> list[tuple[tuple, str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


# Service warning and runtime projection boundaries.


def test_minimal_config_returns_complete_normalized_plan(
    write_config: Callable[[str], Path],
) -> None:
    """Turn a minimal public config file into the normalized render plan."""
    plan = load_validate_plan(write_config(MINIMAL_CONFIG))

    assert isinstance(plan, RenderPlan)
    assert plan.base_image == "nvidia/cuda:12.9.2-cudnn-devel-ubuntu24.04"
    assert plan.paths.comfyui == "/workspace/ComfyUI"
    assert plan.pytorch.wheel_tag == "cu129"
    assert plan.files.downloader.default == "aria2"


def test_result_bakes_host_async_download_mode_with_scheduling_warnings(
    write_config: Callable[[str], Path],
) -> None:
    """Runtime download-mode fields are baked into runtime defaults."""
    document = (
        MINIMAL_CONFIG
        + """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
download_mode = "async"
"""
    )

    result = load_validate_plan_result(write_config(document))

    assert isinstance(result.plan, RenderPlan)
    assert [
        (warning.path, warning.code, warning.severity) for warning in result.warnings
    ] == [
        (
            ("cdh", "default_download_mode"),
            "host_build.download_scheduling_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("files", 0, "download_mode"),
            "host_build.download_scheduling_ignored",
            DiagnosticSeverity.WARNING,
        ),
    ]
    assert result.runtime_config.files[0].download_mode == "async"
    assert result.runtime_config.config.cdh.default_download_mode == "async"


def test_plan_only_loader_accepts_host_download_mode_fields(
    write_config: Callable[[str], Path],
) -> None:
    """Preserve the existing load_validate_plan return contract."""
    document = (
        MINIMAL_CONFIG
        + """
[cdh]
default_download_mode = "async"
"""
    )

    assert isinstance(load_validate_plan(write_config(document)), RenderPlan)


@pytest.mark.parametrize(
    ("cdh_fragment", "expected_warning"),
    [
        ("", False),
        ('download_failure_policy = "fail"\n', False),
        ('download_failure_policy = "continue"\n', True),
    ],
)
def test_explicit_continue_failure_policy_warns_for_build_time_files(
    write_config: Callable[[str], Path],
    cdh_fragment: str,
    expected_warning: bool,
) -> None:
    """Warn only when explicit continue policy can affect build-time files."""
    document = (
        MINIMAL_CONFIG
        + f"""
[cdh]
{cdh_fragment}

[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
"""
    )

    result = load_validate_plan_result(write_config(document))

    assert bool(result.warnings) is expected_warning
    if expected_warning:
        warning = result.warnings[0]
        assert warning.path == ("cdh", "download_failure_policy")
        assert warning.code == "host_build.download_failure_policy_continue"
        assert warning.severity == DiagnosticSeverity.WARNING


def test_non_empty_host_ssh_password_warns_without_leaking_password(
    write_config: Callable[[str], Path],
) -> None:
    """Warn that baked SSH passwords can be exposed without printing the value."""
    document = (
        MINIMAL_CONFIG
        + """
[system.ssh]
password = "super-secret"
"""
    )

    result = load_validate_plan_result(write_config(document))

    assert [(item.path, item.code, item.severity) for item in result.warnings] == [
        (
            ("system", "ssh", "password"),
            "host_build.ssh_password_baked",
            DiagnosticSeverity.WARNING,
        )
    ]
    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in result.warnings
    )
    assert "super-secret" not in payload
    assert result.runtime_config.config.system.ssh.password == "super-secret"


def test_invalid_host_ssh_public_key_fails_with_stable_diagnostic_no_password_leak(
    write_config: Callable[[str], Path],
) -> None:
    document = (
        MINIMAL_CONFIG
        + f"""
[system.ssh]
password = "super-secret"
pub_keys = ["{TRUNCATED_SSH_KEY}"]
"""
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan_result(write_config(document))

    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in raised.value.diagnostics
    )
    assert _identities(raised.value) == [
        (("system", "ssh", "pub_keys", 0), "ssh.invalid_public_key")
    ]
    assert "super-secret" not in payload


def test_invalid_host_file_download_mode_fails_before_runtime_projection(
    write_config: Callable[[str], Path],
) -> None:
    """Keep runtime file download mode limited to supported enum values."""
    document = (
        MINIMAL_CONFIG
        + """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
download_mode = "parallel"
"""
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan_result(write_config(document))

    assert _identities(raised.value) == [
        (("files", 0, "download_mode"), "schema.literal_error")
    ]


@pytest.mark.parametrize(
    ("directory", "code"),
    [
        ("models/", "file.trailing_slash"),
        ("models//checkpoints", "file.empty_directory_segment"),
    ],
)
def test_runtime_incompatible_host_file_dirs_fail_before_projection(
    write_config: Callable[[str], Path],
    directory: str,
    code: str,
) -> None:
    """Reject host file dirs that would make baked runtime config invalid."""
    document = (
        MINIMAL_CONFIG
        + f"""
[[files]]
url = "https://example.com/model.bin"
dir = "{directory}"
filename = "model.bin"
"""
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan_result(write_config(document))

    assert _identities(raised.value) == [(("files", 0, "dir"), code)]


def test_full_config_and_hooks_use_explicit_scripts_directory(
    write_config: Callable[[str], Path], tmp_path: Path
) -> None:
    """Resolve hooks and optional sections through the service entry point."""
    scripts_dir = tmp_path / "hooks"
    scripts_dir.mkdir()
    (scripts_dir / "before.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    document = (
        MINIMAL_CONFIG.replace('version = "latest"', 'version = "v1.2.3"')
        + """
[system]
workspace = "/srv"

[[comfyui.custom_nodes]]
type = "registry"
id = "example"
pre_install_scripts = ["before.sh"]

[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
downloader = "httpx"
"""
    )

    plan = load_validate_plan(write_config(document), scripts_dir=scripts_dir)

    assert plan.paths.comfyui == "/srv/ComfyUI"
    assert plan.comfyui.version == "1.2.3"
    assert plan.custom_nodes.scripts_source_dir == scripts_dir.resolve()
    assert plan.files.items[0].target == "/srv/ComfyUI/models/model.bin"


def test_no_hooks_do_not_require_or_inspect_scripts_directory(
    write_config: Callable[[str], Path], tmp_path: Path
) -> None:
    """Do not require a scripts directory when the plan has no hooks."""
    missing = tmp_path / "does-not-exist"

    plan = load_validate_plan(write_config(MINIMAL_CONFIG), scripts_dir=missing)

    assert plan.custom_nodes.has_hooks is False
    assert plan.custom_nodes.scripts_source_dir is None


# Multi-file merge behavior.


def test_multi_file_merge_applies_cli_order_and_keyed_overrides(
    tmp_path: Path,
) -> None:
    """Merge partial raw TOML files in order before validation and planning."""
    base = tmp_path / "base.toml"
    profile = tmp_path / "profile.toml"
    local = tmp_path / "local.toml"
    base.write_text(
        MINIMAL_CONFIG
        + """
[system]
workspace = "/base"
extra_packages = ["base-package"]

[system.env]
PROFILE = "base"

[python]
extra_packages = ["base-python"]

[cdh]
default_downloader = "aria2"

[[comfyui.custom_nodes]]
type = "registry"
id = "same-registry"
version = "1.0.0"
pre_install_scripts = ["base.sh"]

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/same.git"
ref = "old"

[[files]]
url = "https://example.com/base.bin"
dir = "models"
filename = "same.bin"
downloader = "aria2"
""",
        encoding="utf-8",
    )
    profile.write_text(
        """
[system]
extra_packages = ["profile-package"]

[system.env]
PROFILE = "profile"
EXTRA = "yes"

[python]
extra_packages = ["profile-python"]

[comfyui]
listen = "127.0.0.1"
port = 8190
extra_args = ["--cpu"]

[[comfyui.custom_nodes]]
type = "registry"
id = "same-registry"
version = "2.0.0"
pre_install_scripts = []

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/new.git"
target_dir = "new-node"

[[files]]
url = "https://example.com/profile.bin"
dir = "models"
filename = "same.bin"
overwrite = true
downloader = "httpx"

[[files]]
url = "https://example.com/extra.bin"
dir = "models"
filename = "extra.bin"
""",
        encoding="utf-8",
    )
    local.write_text(
        """
[cdh.downloader.httpx]
retries = 7
""",
        encoding="utf-8",
    )

    plan = load_validate_plan([base, profile, local])

    assert plan.paths.workspace == "/base"
    assert plan.os_packages[-1:] == ("profile-package",)
    assert [(item.name, item.value) for item in plan.environment] == [
        ("PROFILE", "profile"),
        ("EXTRA", "yes"),
    ]
    assert plan.python.extra_packages == ("profile-python",)
    assert plan.comfyui.listen == "127.0.0.1"
    assert plan.comfyui.port == 8190
    assert plan.comfyui.extra_arguments == ("--cpu",)
    assert [(node.type, node.target) for node in plan.custom_nodes.items] == [
        ("registry", "same-registry@2.0.0"),
        ("git", "https://example.com/same.git@old"),
        ("git", "https://example.com/new.git"),
    ]
    first_file, second_file = plan.files.items
    assert first_file.filename == "same.bin"
    assert first_file.url == "https://example.com/profile.bin"
    assert first_file.overwrite is True
    assert first_file.downloader == "httpx"
    assert second_file.filename == "extra.bin"
    assert second_file.downloader == "aria2"
    assert plan.files.downloader.httpx.retries == 7


def test_multi_file_empty_arrays_reset_accumulated_special_lists(
    tmp_path: Path,
) -> None:
    """Use TOML-native empty arrays to clear accumulated nodes and files."""
    base = tmp_path / "base.toml"
    reset = tmp_path / "reset.toml"
    base.write_text(
        MINIMAL_CONFIG
        + """
[[comfyui.custom_nodes]]
type = "registry"
id = "node"

[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
        encoding="utf-8",
    )
    reset.write_text(
        """
files = []

[comfyui]
custom_nodes = []
""",
        encoding="utf-8",
    )

    plan = load_validate_plan([base, reset])

    assert plan.custom_nodes.items == ()
    assert plan.files.items == ()


def test_multi_file_merge_preserves_duplicate_custom_node_validation(
    tmp_path: Path,
) -> None:
    """Do not collapse duplicate keys from the same override layer."""
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(MINIMAL_CONFIG, encoding="utf-8")
    override.write_text(
        """
[[comfyui.custom_nodes]]
type = "registry"
id = "duplicate"
version = "1.0.0"

[[comfyui.custom_nodes]]
type = "registry"
id = "duplicate"
version = "2.0.0"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan([base, override])

    assert _identities(raised.value) == [
        (
            ("comfyui", "custom_nodes", 1, "id"),
            "custom_node.duplicate_registry_id",
        )
    ]


def test_multi_file_file_items_require_filename_for_merge_identity(
    tmp_path: Path,
) -> None:
    """Missing file filename remains a normal schema error after merge."""
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    base.write_text(MINIMAL_CONFIG, encoding="utf-8")
    override.write_text(
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan([base, override])

    assert _identities(raised.value) == [(("files", 0, "filename"), "schema.missing")]


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
def test_service_rejects_cdh_controlled_comfyui_extra_args(
    write_config: Callable[[str], Path],
    argument: str,
) -> None:
    """Return stable user-facing diagnostics for cdh-owned startup flags."""
    document = (
        MINIMAL_CONFIG
        + f"""
extra_args = ["--cpu", "{argument}"]
"""
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan(write_config(document))

    assert _identities(raised.value) == [
        (("comfyui", "extra_args", 1), "comfyui.controlled_extra_arg")
    ]
    assert argument.split("=", maxsplit=1)[0] in raised.value.diagnostics[0].message


# Diagnostic normalization.


@pytest.mark.parametrize(
    ("path_factory", "code"),
    [
        (lambda root: root / "missing.toml", "config.file_not_found"),
        (lambda root: root, "config.not_a_file"),
    ],
)
def test_file_io_errors_become_stable_diagnostics(
    tmp_path: Path, path_factory, code: str
) -> None:
    """Normalize missing and non-file inputs to stable service diagnostics."""
    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan(path_factory(tmp_path))

    assert _identities(raised.value) == [((), code)]


def test_multi_file_read_errors_include_the_failing_source_path(
    write_config: Callable[[str], Path], tmp_path: Path
) -> None:
    """Keep layered read failures attributable to the failing config file."""
    base = write_config(MINIMAL_CONFIG)
    missing = tmp_path / "missing.toml"

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan([base, missing])

    assert _identities(raised.value) == [((), "config.file_not_found")]
    assert str(missing) in raised.value.diagnostics[0].message


def test_toml_decode_error_becomes_a_document_diagnostic(
    write_config: Callable[[str], Path],
) -> None:
    """Convert malformed TOML into a safe document-level diagnostic."""
    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan(write_config("[compute_platform\n"))

    assert _identities(raised.value) == [((), "toml.invalid_document")]
    assert "line 1" in raised.value.diagnostics[0].message


def test_multi_file_toml_errors_include_the_failing_source_path(
    write_config: Callable[[str], Path], tmp_path: Path
) -> None:
    """Identify the malformed override file during layered loading."""
    base = write_config(MINIMAL_CONFIG)
    malformed = tmp_path / "malformed.toml"
    malformed.write_text("[compute_platform\n", encoding="utf-8")

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan([base, malformed])

    assert _identities(raised.value) == [((), "toml.invalid_document")]
    assert str(malformed) in raised.value.diagnostics[0].message


def test_invalid_utf8_becomes_a_safe_document_diagnostic(tmp_path: Path) -> None:
    """Reject invalid UTF-8 without exposing decoder tracebacks."""
    path = tmp_path / "config.toml"
    path.write_bytes(b'[compute_platform]\ntype = "cuda"\n\xff')

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan(path)

    assert _identities(raised.value) == [((), "toml.invalid_encoding")]
    assert raised.value.diagnostics[0].message == (
        "configuration file must be valid UTF-8"
    )


def test_structural_errors_are_aggregated_with_exact_user_paths(
    write_config: Callable[[str], Path],
) -> None:
    """Aggregate schema errors with stable paths from the user document."""
    document = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[system.env]
PATH = 1

[pytorch]
extra_packages = []

[comfyui]
version = "latest"

[[files]]
url = "https://example.com/file"
dir = 7

[[comfyui.custom_nodes]]
type = "archive"
id = "node"
"""

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan(write_config(document))

    assert _identities(raised.value) == [
        (("system", "env", "PATH"), "schema.string_type"),
        (("pytorch", "version"), "schema.missing"),
        (
            ("comfyui", "custom_nodes", 0),
            "schema.union_tag_invalid",
        ),
        (("files", 0, "dir"), "schema.string_type"),
        (("files", 0, "filename"), "schema.missing"),
    ]


@pytest.mark.parametrize(
    ("node", "expected_path"),
    [
        (
            'type = "registry"\nid = "node"\nurl = "https://example.com/node.git"',
            ("comfyui", "custom_nodes", 0, "url"),
        ),
        (
            'type = "git"\nurl = "https://example.com/node.git"\nversion = "1"',
            ("comfyui", "custom_nodes", 0, "version"),
        ),
    ],
)
def test_discriminated_union_branch_tags_are_removed_from_public_paths(
    write_config: Callable[[str], Path], node: str, expected_path: tuple
) -> None:
    """Hide internal union branch tags from custom-node diagnostics."""
    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan(
            write_config(MINIMAL_CONFIG + "\n[[comfyui.custom_nodes]]\n" + node)
        )

    assert [item.path for item in raised.value.diagnostics] == [expected_path]
    assert all(
        branch not in item.path
        for item in raised.value.diagnostics
        for branch in ("registry", "git")
    )


@pytest.mark.parametrize("field_name", ["registry", "git"])
def test_union_path_normalization_preserves_user_fields_named_like_branches(
    write_config: Callable[[str], Path], field_name: str
) -> None:
    """Keep real user field names even when they match union branch labels."""
    node = f'type = "registry"\nid = "node"\n{field_name} = "user value"'

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan(
            write_config(MINIMAL_CONFIG + "\n[[comfyui.custom_nodes]]\n" + node)
        )

    assert [item.path for item in raised.value.diagnostics] == [
        ("comfyui", "custom_nodes", 0, field_name)
    ]


def test_independent_business_errors_use_same_service_error_and_order(
    write_config: Callable[[str], Path],
) -> None:
    """Return deterministic business-rule diagnostics after schema validation."""
    document = MINIMAL_CONFIG.replace('type = "cuda"', 'type = "rocm"').replace(
        '[pytorch]\nversion = "2.10"',
        """[system]
workspace = "relative"

[system.env]
PATH = "unsafe"

[pytorch]
version = "2.10"

[[files]]
url = "https://"
dir = "/absolute"
filename = "file.bin"
""",
    )

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan(write_config(document))

    assert _identities(raised.value) == [
        (("compute_platform", "type"), "compute_platform.unsupported_backend"),
        (("system", "workspace"), "system.path_not_absolute"),
        (("system", "env", "PATH"), "system.managed_env_override"),
        (("files", 0, "url"), "file.invalid_url"),
        (("files", 0, "dir"), "file.absolute_directory"),
    ]


def test_hook_errors_use_default_scripts_dir_relative_to_current_working_directory(
    write_config: Callable[[str], Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve the default scripts directory from the caller working directory."""
    document = (
        MINIMAL_CONFIG
        + """
[[comfyui.custom_nodes]]
type = "registry"
id = "node"
pre_install_scripts = ["hook.sh"]
"""
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigurationServiceError) as raised:
        load_validate_plan(write_config(document))

    assert _identities(raised.value) == [
        (("scripts_dir",), "hook.scripts_dir_not_directory")
    ]

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "hook.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    plan = load_validate_plan(write_config(document))
    assert plan.custom_nodes.scripts_source_dir == scripts_dir.resolve()


def test_successful_service_call_does_not_modify_the_input_tree(
    write_config: Callable[[str], Path], tmp_path: Path
) -> None:
    """Keep validation/planning reads free of filesystem writes."""
    path = write_config(MINIMAL_CONFIG)
    before = {
        item.relative_to(tmp_path): item.stat().st_mtime_ns
        for item in tmp_path.rglob("*")
    }

    load_validate_plan(path)

    after = {
        item.relative_to(tmp_path): item.stat().st_mtime_ns
        for item in tmp_path.rglob("*")
    }
    assert after == before
