"""Strict effective runtime validation and diagnostic contracts."""

from pathlib import Path

import pytest

from comfyui_docker_helper.config import (
    DiagnosticSeverity,
    RuntimeConfigurationError,
    load_runtime_config,
)
from comfyui_docker_helper.config.diagnostics import (
    DiagnosticComparison,
    SourceLocation,
)

VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "test@example"
)
TRUNCATED_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 truncated"


def _write(path: Path, document: str) -> Path:
    path.write_text(document, encoding="utf-8")
    return path


def _identities(error: RuntimeConfigurationError) -> list[tuple[tuple, str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


def test_runtime_downloader_credentials_are_independent_and_value_lazy(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_downloader = "httpx"

[[cdh.downloader.credentials]]
match = "https://example.test/private/"
type = "bearer"
token = { secret = "runtime_read" }

[secrets.runtime_read]
file = "/run/secrets/runtime-token"

[[files]]
type = "http"
url = "https://example.test/private/model.bin?download=1"
target_dir = "models"
filename = "model.bin"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
        environ={},
    )

    assert result.config.secrets["runtime_read"].file == "/run/secrets/runtime-token"


def test_runtime_authenticated_aria2_fails_with_security_remediation(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "runtime.toml",
        """
[[cdh.downloader.credentials]]
match = "https://example.test/private/"
type = "bearer"
token = { secret = "runtime_read" }

[secrets.runtime_read]
env = "RUNTIME_TOKEN"

[[files]]
type = "http"
url = "https://example.test/private/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
        )

    diagnostic = next(
        item
        for item in raised.value.diagnostics
        if item.code == "downloader_credential.httpx_required"
    )
    assert diagnostic.path == ("files", 0, "downloader")
    assert "security" in diagnostic.message.lower()
    assert diagnostic.hint is not None and "httpx" in diagnostic.hint


def test_runtime_secret_file_requires_absolute_container_path(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "runtime.toml",
        """
[[cdh.downloader.credentials]]
match = "https://example.test/private/"
type = "bearer"
token = { secret = "runtime_read" }

[secrets.runtime_read]
file = "relative/token"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
        )

    assert any(item.code == "secret.invalid_file" for item in raised.value.diagnostics)


def test_invalid_ssh_public_keys_fail_without_leaking_password(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
password = "super-secret"
pub_keys = ["not-a-key"]
""",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in raised.value.diagnostics
    )
    assert _identities(raised.value) == [
        (("system", "ssh", "pub_keys", 0), "ssh.invalid_public_key")
    ]
    assert "super-secret" not in payload


def test_truncated_base64_valid_ssh_public_key_fails(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[system.ssh]
pub_keys = ["{TRUNCATED_SSH_KEY}"]
""",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(raised.value) == [
        (("system", "ssh", "pub_keys", 0), "ssh.invalid_public_key")
    ]


def test_embedded_newline_ssh_public_key_fails_without_leaking_key(
    tmp_path: Path,
) -> None:
    injected = f"{VALID_SSH_KEY}\nssh-ed25519 injected"
    mounted = _write(
        tmp_path / "mounted.toml",
        f'''
[system.ssh]
password = "super-secret"
pub_keys = ["""{injected}"""]
''',
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in raised.value.diagnostics
    )
    assert _identities(raised.value) == [
        (("system", "ssh", "pub_keys", 0), "ssh.invalid_public_key")
    ]
    assert "super-secret" not in payload
    assert VALID_SSH_KEY not in payload
    assert "injected" not in payload


def test_nul_ssh_pub_key_env_fails_without_leaking_key(tmp_path: Path) -> None:
    injected = f"{VALID_SSH_KEY}\x00comment"

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={
                "SSH_PASSWORD": "env-super-secret",
                "SSH_PUB_KEY": injected,
            },
        )

    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in raised.value.diagnostics
    )
    assert _identities(raised.value) == [
        (("env", "SSH_PUB_KEY"), "env.invalid_ssh_pub_key")
    ]
    assert "env-super-secret" not in payload
    assert VALID_SSH_KEY not in payload


def test_invalid_ssh_pub_key_env_fails_without_leaking_password(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={
                "SSH_PASSWORD": "env-super-secret",
                "SSH_PUB_KEY": TRUNCATED_SSH_KEY,
            },
        )

    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in raised.value.diagnostics
    )
    assert _identities(raised.value) == [
        (("env", "SSH_PUB_KEY"), "env.invalid_ssh_pub_key")
    ]
    assert "env-super-secret" not in payload


@pytest.mark.parametrize(
    ("document", "path", "code"),
    [
        ('system = "invalid"\n', ("system",), "schema.model_type"),
        ("[system]\nssh = []\n", ("system", "ssh"), "schema.model_type"),
        (
            "[system.ssh]\npub_keys = 1\n",
            ("system", "ssh", "pub_keys"),
            "schema.list_type",
        ),
    ],
)
def test_ssh_pub_key_env_does_not_bypass_effective_structure_validation(
    tmp_path: Path,
    document: str,
    path: tuple[str, ...],
    code: str,
) -> None:
    mounted = _write(tmp_path / "mounted.toml", document)

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={"SSH_PUB_KEY": VALID_SSH_KEY},
        )

    assert _identities(raised.value) == [(path, code)]
    diagnostic = raised.value.diagnostics[0]
    assert isinstance(diagnostic.source_context, SourceLocation)
    assert diagnostic.source_context.source.label == str(mounted)
    payload = f"{diagnostic.path} {diagnostic.code} {diagnostic.message}"
    assert VALID_SSH_KEY not in payload


@pytest.mark.parametrize("value", ["0", "-0.1", "-2", "nan", "inf", '"8"', "true"])
def test_invalid_shutdown_timeout_toml_fails_schema_validation(
    tmp_path: Path,
    value: str,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"[cdh]\nshutdown_timeout = {value}\n",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
        )

    assert [item.path for item in raised.value.diagnostics] == [
        ("cdh", "shutdown_timeout")
    ]


# Host-only build-time settings may appear in mounted files but must not affect
# container runtime state.
def test_known_host_only_runtime_config_warns_and_is_ignored(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[system]
workspace = "/srv"

[python]
version = "3.12"

[pytorch]
version = "2.10"

[build]
tags = ["example:dev"]

[cdh]
local_file_mode = "clone"

[comfyui]
version = "latest"
install_cli = false
install_manager = true
listen = "127.0.0.1"

[[comfyui.custom_nodes]]
type = "registry"
id = "node"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
    )

    assert result.config.comfyui.listen == "127.0.0.1"
    assert [(item.path, item.code, item.severity) for item in result.warnings] == [
        (
            ("compute_platform",),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("system", "workspace"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (("python",), "runtime.host_only_ignored", DiagnosticSeverity.WARNING),
        (("pytorch",), "runtime.host_only_ignored", DiagnosticSeverity.WARNING),
        (("build",), "runtime.host_only_ignored", DiagnosticSeverity.WARNING),
        (
            ("cdh", "local_file_mode"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("comfyui", "version"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("comfyui", "install_cli"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("comfyui", "install_manager"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("comfyui", "custom_nodes"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
    ]


def test_unknown_runtime_sections_and_fields_fail(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[readiness]
timeout = 60

[comfyui]
unknown = true

[cdh]
unexpected = "value"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("comfyui", "unknown"), "schema.extra_forbidden"),
        (("cdh", "unexpected"), "schema.extra_forbidden"),
        (("readiness",), "schema.extra_forbidden"),
    ]


# Runtime file config coverage preserves effective item shape plus authored
# files.N diagnostics before runtime_files turns entries into executable plans.
def test_runtime_file_entries_are_accepted_and_recorded(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
    )

    assert result.files == (
        {
            "type": "http",
            "url": "https://example.com/model.bin",
            "target_dir": "models",
            "filename": "model.bin",
        },
    )


def test_runtime_file_merge_uses_canonical_target_and_returns_canonical_dir(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[[files]]
type = "http"
url = "https://example.com/base.bin"
target_dir = "models//checkpoints/"
filename = "model.bin"
overwrite = false
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
target_dir = "./models/checkpoints"
filename = "model.bin"
overwrite = true
""",
    )

    result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=mounted,
    )

    assert result.files == (
        {
            "type": "http",
            "url": "https://example.com/base.bin",
            "target_dir": "models/checkpoints",
            "filename": "model.bin",
            "overwrite": True,
        },
    )


def test_runtime_file_url_accepts_valid_userinfo(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://user:password@example.com/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
    )

    assert result.files[0]["url"] == "https://user:password@example.com/model.bin"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("url", "https://example.com/model\\u007f.bin", "runtime_file.invalid_url"),
        ("target_dir", "models\\u007fescape", "runtime_file.control_character"),
        ("filename", "model\\u007f.bin", "runtime_file.invalid_filename"),
    ],
)
def test_runtime_file_domains_reject_control_characters(
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    values = {
        "url": "https://example.com/model.bin",
        "target_dir": "models",
        "filename": "model.bin",
    }
    values[field] = value
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[[files]]
type = "http"
url = "{values["url"]}"
target_dir = "{values["target_dir"]}"
filename = "{values["filename"]}"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [(("files", 0, field), code)]


def test_runtime_file_non_http_url_fails_runtime_validation(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "ftp://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 0, "url"), "runtime_file.invalid_url")
    ]


def test_runtime_file_rejects_reserved_staging_final_leaf(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = ".cdh-staging"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 0, "filename"), "runtime_file.invalid_filename")
    ]


def test_invalid_mounted_runtime_file_after_baked_reports_effective_and_source_paths(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[[files]]
type = "http"
url = "https://example.com/baked.bin"
target_dir = "models"
filename = "baked.bin"
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "ftp://example.com/mounted.bin"
target_dir = "models"
filename = "mounted.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(baked_config_path=baked, mounted_config_path=mounted)

    assert _identities(error.value) == [
        (("files", 1, "url"), "runtime_file.invalid_url")
    ]
    context = error.value.diagnostics[0].source_context
    assert isinstance(context, SourceLocation)
    assert context.source.label == str(mounted)
    assert context.path == ("files", 0, "url")


def test_multiple_invalid_runtime_file_items_keep_authored_indexes(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://example.com/a.bin"
target_dir = "/models"
filename = "a.bin"

[[files]]
type = "http"
url = "https://example.com/b.bin"
target_dir = "models"
filename = "nested/b.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 0, "target_dir"), "runtime_file.absolute_directory"),
        (("files", 1, "filename"), "runtime_file.invalid_filename"),
    ]


def test_runtime_file_async_download_mode_is_accepted(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
download_mode = "async"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
    )

    assert result.files[0]["download_mode"] == "async"


def test_runtime_file_invalid_download_mode_fails_schema_validation(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
download_mode = "parallel"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 0, "download_mode"), "schema.literal_error")
    ]


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("download_max_attempts", "0", "schema.greater_than_equal"),
        ("download_max_attempts", "-1", "schema.greater_than_equal"),
        ("download_failure_policy", '"skip"', "schema.literal_error"),
    ],
)
def test_invalid_runtime_download_policy_values_fail_schema_validation(
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[cdh]
{field} = {value}
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [(("cdh", field), code)]


def test_runtime_file_unknown_field_fails_schema_validation(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
unexpected = true
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 0, "unexpected"), "schema.extra_forbidden")
    ]


def test_runtime_file_merge_preserves_current_baked_mounted_contract(
    tmp_path: Path,
) -> None:
    appended_baked = _write(
        tmp_path / "baked.toml",
        """
[[files]]
type = "http"
url = "https://example.com/baked.bin"
target_dir = "models"
filename = "baked.bin"
""",
    )
    appended_mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://example.com/mounted.bin"
target_dir = "models"
filename = "mounted.bin"
downloader = "httpx"
""",
    )

    appended = load_runtime_config(
        baked_config_path=appended_baked,
        mounted_config_path=appended_mounted,
    )

    assert appended.files == (
        {
            "type": "http",
            "url": "https://example.com/baked.bin",
            "target_dir": "models",
            "filename": "baked.bin",
        },
        {
            "type": "http",
            "url": "https://example.com/mounted.bin",
            "target_dir": "models",
            "filename": "mounted.bin",
            "downloader": "httpx",
        },
    )

    override_baked = _write(
        tmp_path / "override-baked.toml",
        """
[[files]]
type = "http"
url = "https://example.com/baked.bin"
target_dir = "models"
filename = "model.bin"
overwrite = false
downloader = "aria2"
""",
    )
    override_mounted = _write(
        tmp_path / "override-mounted.toml",
        """
[[files]]
type = "http"
target_dir = "models"
filename = "model.bin"
overwrite = true
""",
    )

    overridden = load_runtime_config(
        baked_config_path=override_baked,
        mounted_config_path=override_mounted,
    )

    assert overridden.files == (
        {
            "type": "http",
            "url": "https://example.com/baked.bin",
            "target_dir": "models",
            "filename": "model.bin",
            "overwrite": True,
            "downloader": "aria2",
        },
    )

    reset_baked = _write(
        tmp_path / "reset-baked.toml",
        """
[[files]]
type = "http"
url = "https://example.com/baked.bin"
target_dir = "models"
filename = "baked.bin"
""",
    )
    reset_mounted = _write(tmp_path / "reset-mounted.toml", "files = []\n")

    reset = load_runtime_config(
        baked_config_path=reset_baked,
        mounted_config_path=reset_mounted,
    )

    assert reset.files == ()


def test_invalid_runtime_values_may_be_replaced_before_effective_validation(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[comfyui]
port = 70000
extra_args = [1]

[cdh]
default_downloader = "invalid"
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[comfyui]
port = 8288
extra_args = []

[cdh]
default_downloader = "httpx"
""",
    )

    result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=mounted,
    )

    assert result.config.comfyui.port == 8288
    assert result.config.comfyui.extra_args == []
    assert result.config.cdh.default_downloader == "httpx"


def test_runtime_file_invalid_fields_may_be_repaired_by_later_layer(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[[files]]
type = "http"
url = "ftp://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
checksum = "invalid"
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
checksum = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
""",
    )

    result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=mounted,
    )

    assert result.files == (
        {
            "type": "http",
            "url": "https://example.com/model.bin",
            "target_dir": "models",
            "filename": "model.bin",
            "checksum": (
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
        },
    )


def test_runtime_file_local_source_is_rejected_by_strict_admission(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "local"
path = "/run/seeds/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(raised.value) == [
        (("files", 0, "type"), "schema.literal_error"),
        (("files", 0, "path"), "schema.extra_forbidden"),
    ]


def test_invalid_runtime_files_may_be_reset_before_effective_validation(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[[files]]
type = "http"
url = "ftp://example.com/model.bin"
target_dir = "/models"
filename = "nested/model.bin"
""",
    )
    mounted = _write(tmp_path / "mounted.toml", "files = []\n")

    result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=mounted,
    )

    assert result.files == ()


def test_runtime_file_missing_effective_url_is_attributed_to_authored_item(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
target_dir = "models"
filename = "model.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [(("files", 0, "url"), "schema.missing")]
    context = error.value.diagnostics[0].source_context
    assert isinstance(context, SourceLocation)
    assert context.source.label == str(mounted)
    assert context.path == ("files", 0)


# One three-way collision owns pairwise source attribution and value non-disclosure.
def test_three_runtime_file_duplicates_compare_first_with_each_later_item(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://example.com/one.bin"
target_dir = "models"
filename = "model.bin"

[[files]]
type = "http"
url = "https://example.com/two.bin"
target_dir = "models"
filename = "model.bin"

[[files]]
type = "http"
url = "https://example.com/three.bin"
target_dir = "models"
filename = "model.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 1, "filename"), "runtime_file.duplicate_target"),
        (("files", 2, "filename"), "runtime_file.duplicate_target"),
    ]
    contexts = [diagnostic.source_context for diagnostic in error.value.diagnostics]
    assert all(isinstance(context, DiagnosticComparison) for context in contexts)
    assert [
        (
            context.earlier.location.path,
            context.later.location.path,
        )
        for context in contexts
        if isinstance(context, DiagnosticComparison)
    ] == [
        (("files", 0, "filename"), ("files", 1, "filename")),
        (("files", 0, "filename"), ("files", 2, "filename")),
    ]
    assert all(
        context.earlier.display_value is None and context.later.display_value is None
        for context in contexts
        if isinstance(context, DiagnosticComparison)
    )


def test_cross_layer_runtime_file_ambiguity_preserves_authored_sources(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[[files]]
type = "http"
url = "https://example.com/base.bin"
target_dir = "models"
filename = "model.bin"
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
type = "http"
url = "https://example.com/later-one.bin"
target_dir = "models"
filename = "model.bin"

[[files]]
type = "http"
url = "https://example.com/later-two.bin"
target_dir = "models"
filename = "model.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(baked_config_path=baked, mounted_config_path=mounted)

    assert [diagnostic.path for diagnostic in error.value.diagnostics] == [
        ("files", 1, "filename"),
        ("files", 2, "filename"),
    ]
    contexts = [diagnostic.source_context for diagnostic in error.value.diagnostics]
    assert [
        (
            context.earlier.location.source.label,
            context.earlier.location.path,
            context.later.location.source.label,
            context.later.location.path,
        )
        for context in contexts
        if isinstance(context, DiagnosticComparison)
    ] == [
        (str(baked), ("files", 0, "filename"), str(mounted), ("files", 0, "filename")),
        (str(baked), ("files", 0, "filename"), str(mounted), ("files", 1, "filename")),
    ]


# Downloader validation keeps runtime-only backend tuning strict at load time.
def test_invalid_baked_aria2_backend_values_fail_runtime_validation(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[cdh.downloader.aria2]
rpc_port = 0
split = 0
max_connection_per_server = 0
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=baked,
            mounted_config_path=tmp_path / "missing-mounted.toml",
        )

    assert _identities(error.value) == [
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


def test_invalid_mounted_httpx_backend_values_fail_runtime_validation(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh.downloader.httpx]
timeout = 0
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (
            ("cdh", "downloader", "httpx", "timeout"),
            "cdh.downloader.httpx_timeout_not_positive",
        ),
    ]


@pytest.mark.parametrize(
    ("document", "path", "code"),
    [
        (
            'listen = "host\\u007fpart"',
            ("comfyui", "listen"),
            "comfyui.invalid_listen",
        ),
        (
            'extra_args = ["--cpu\\u007fprobe"]',
            ("comfyui", "extra_args", 0),
            "comfyui.invalid_extra_arg",
        ),
    ],
)
def test_runtime_comfyui_argv_rejects_control_characters(
    tmp_path: Path,
    document: str,
    path: tuple[str | int, ...],
    code: str,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[comfyui]
{document}
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [(path, code)]


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        (
            "CDH_DEFAULT_DOWNLOADER",
            "curl",
            (("cdh", "default_downloader"), "schema.literal_error"),
        ),
        (
            "CDH_DEFAULT_DOWNLOAD_MODE",
            "parallel",
            (("cdh", "default_download_mode"), "schema.literal_error"),
        ),
        (
            "CDH_DOWNLOAD_FAILURE_POLICY",
            "skip",
            (("cdh", "download_failure_policy"), "schema.literal_error"),
        ),
    ],
)
def test_invalid_env_enum_values_fail_runtime_validation(
    tmp_path: Path,
    name: str,
    value: str,
    expected: tuple[tuple, str],
) -> None:
    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={name: value},
        )

    assert _identities(error.value) == [expected]


# ComfyUI process ownership stays with the entrypoint for listen, port, and
# auto-launch flags even when extra args come from runtime config or env.
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
def test_runtime_extra_args_reject_cdh_controlled_flags(
    tmp_path: Path,
    argument: str,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[comfyui]
extra_args = ["--cpu", "{argument}"]
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("comfyui", "extra_args", 1), "comfyui.controlled_extra_arg")
    ]


def test_env_extra_args_reject_cdh_controlled_flags(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={"CDH_COMFYUI_EXTRA_ARGS": "--cpu --port=8190"},
        )

    assert _identities(error.value) == [
        (("comfyui", "extra_args", 1), "comfyui.controlled_extra_arg")
    ]
