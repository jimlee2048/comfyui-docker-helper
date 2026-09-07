"""Recording configuration grammar, precedence and two-stage admission contracts."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config.authored.models import FinalCdhConfig
from comfyui_docker_helper.config.diagnostics import SourceLocation
from comfyui_docker_helper.config.logs import RuntimeLogSettings
from comfyui_docker_helper.config.runtime import config as runtime_config
from comfyui_docker_helper.config.runtime.config import (
    RuntimeConfigurationError,
    load_runtime_config,
    load_runtime_config_from_sources,
    load_runtime_log_settings,
    read_runtime_config_sources,
)
from comfyui_docker_helper.config.runtime.models import RuntimeCdhConfig


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (20971520, 20971520),
        ("20971520", 20971520),
        ("20m", 20971520),
        ("20MiB", 20971520),
        ("2K", 2048),
        ("2kIb", 2048),
        ("1g", 1024**3),
        ("1GiB", 1024**3),
        ("3B", 3),
    ],
)
def test_size_normalizes_to_integer_bytes(value: object, expected: int) -> None:
    settings = RuntimeLogSettings.model_validate({"max_size": value})
    assert settings.max_size == expected
    assert settings.model_dump()["max_size"] == expected


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        0,
        -1,
        1.5,
        "0",
        "-1",
        "1.5m",
        "10MB",
        "10KB",
        "10GB",
        " 1m",
        "1m ",
        "\u0661m",
    ],
)
def test_size_rejects_ambiguous_or_nonpositive_values(value: object) -> None:
    with pytest.raises(ValidationError):
        RuntimeLogSettings.model_validate({"max_size": value})


@pytest.mark.parametrize("mode", ["none", "memory", "file"])
def test_authored_and_runtime_surfaces_share_immutable_settings(mode: str) -> None:
    document = {"logs": {"mode": mode, "max_size": "20m"}}
    authored = FinalCdhConfig.model_validate(document).logs
    runtime = RuntimeCdhConfig.model_validate(document).logs
    assert authored == runtime
    with pytest.raises(ValidationError, match="frozen"):
        runtime.mode = "none"


@pytest.mark.parametrize("mode", ["off", "disabled", "false", False, None])
def test_mode_requires_a_canonical_string(mode: object) -> None:
    with pytest.raises(ValidationError):
        RuntimeLogSettings.model_validate({"mode": mode})


@pytest.mark.parametrize(
    "directory",
    ["/", "relative", "//server/logs", "/logs/../other", "/logs\n", "C:\\logs"],
)
def test_directory_requires_a_dedicated_posix_path(directory: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeLogSettings(directory=directory)


@pytest.mark.parametrize("count", [True, 0, -1, 1.5, "5"])
def test_file_count_is_a_positive_strict_integer(count: object) -> None:
    with pytest.raises(ValidationError):
        RuntimeLogSettings.model_validate({"max_files": count})


def test_defaults_and_directory_validation_do_not_touch_host_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("container log paths must not be probed on the host")

    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    settings = FinalCdhConfig().logs
    assert settings == RuntimeLogSettings(
        mode="file", directory="/var/log/cdh", max_size=20 * 1024**2, max_files=5
    )
    assert (
        RuntimeCdhConfig.model_validate(
            {"logs": {"directory": "/container/only/logs"}}
        ).logs.directory
        == "/container/only/logs"
    )


def test_sparse_baked_mounted_and_environment_overrides(tmp_path: Path) -> None:
    baked = tmp_path / "baked.toml"
    mounted = tmp_path / "mounted.toml"
    baked.write_text('[cdh.logs]\nmode="memory"\nmax_size="2m"\nmax_files=7\n')
    mounted.write_text('[cdh.logs]\ndirectory="/mounted/logs"\nmax_size="3m"\n')
    settings = load_runtime_config(
        baked_config_path=baked, mounted_config_path=mounted, environ={}
    ).config.cdh.logs
    assert settings == RuntimeLogSettings(
        mode="memory", directory="/mounted/logs", max_size="3m", max_files=7
    )
    sources = read_runtime_config_sources(
        baked_config_path=baked,
        mounted_config_path=mounted,
        environ={
            "CDH_LOG_MODE": "none",
            "CDH_LOG_DIRECTORY": "/environment/logs",
            "CDH_LOG_MAX_SIZE": "4MiB",
            "CDH_LOG_MAX_FILES": "9",
        },
    )
    assert (
        load_runtime_log_settings(sources)
        == load_runtime_config_from_sources(sources).config.cdh.logs
        == RuntimeLogSettings(
            mode="none", directory="/environment/logs", max_size="4m", max_files=9
        )
    )


def test_source_snapshot_is_reused_before_full_admission_and_successors_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mounted = tmp_path / "mounted.toml"
    mounted.write_text('[comfyui]\nport=8288\n[cdh.logs]\nmax_size="1m"\n')
    environ = {"CDH_LOG_MODE": "memory"}
    reads: list[Path] = []
    original = runtime_config._read_runtime_toml

    def read(path: Path, source: object) -> dict:
        reads.append(path)
        return original(path, source)

    monkeypatch.setattr(runtime_config, "_read_runtime_toml", read)
    sources = read_runtime_config_sources(
        baked_config_path=tmp_path / "absent",
        mounted_config_path=mounted,
        environ=environ,
    )
    mounted.write_text('[comfyui]\nport=8388\n[cdh.logs]\nmax_size="2m"\n')
    environ["CDH_LOG_MODE"] = "none"
    logs = load_runtime_log_settings(sources)
    result = load_runtime_config_from_sources(sources)
    assert reads == [mounted]
    assert result.config.cdh.logs == logs
    assert logs.mode == "memory"
    assert logs.max_size == 1024**2
    assert result.config.comfyui.port == 8288
    successor = load_runtime_config(
        baked_config_path=tmp_path / "absent",
        mounted_config_path=mounted,
        environ=sources.environ,
    )
    assert reads == [mounted, mounted]
    assert successor.config.cdh.logs.mode == "memory"
    assert successor.config.cdh.logs.max_size == 2 * 1024**2
    assert successor.config.comfyui.port == 8388


@pytest.mark.parametrize(
    ("document", "environment", "expected_path"),
    [
        ('[comfyui]\nport="invalid"\n', {}, ("comfyui", "port")),
        ("", {"CDH_COMFYUI_PORT": "invalid"}, ("env", "CDH_COMFYUI_PORT")),
        ("", {"SSH_PUB_KEY": "invalid"}, ("env", "SSH_PUB_KEY")),
    ],
    ids=["document-field", "environment-number", "ssh-key"],
)
def test_log_admission_precedes_unrelated_failure_without_bypassing_it(
    tmp_path: Path,
    document: str,
    environment: dict[str, str],
    expected_path: tuple[str, ...],
) -> None:
    mounted = tmp_path / "mounted.toml"
    mounted.write_text(document + '[cdh.logs]\nmode="memory"\n')
    sources = read_runtime_config_sources(
        baked_config_path=tmp_path / "absent",
        mounted_config_path=mounted,
        environ=environment,
    )
    assert load_runtime_log_settings(sources).mode == "memory"
    with pytest.raises(RuntimeConfigurationError) as caught:
        load_runtime_config_from_sources(sources)
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.path == expected_path
    assert isinstance(diagnostic.source_context, SourceLocation)
    assert diagnostic.source_context.source.label == (
        "environment" if environment else str(mounted)
    )


@pytest.mark.parametrize("mode", ["none", "memory"])
def test_inactive_file_fields_remain_strict_with_source_diagnostics(
    tmp_path: Path, mode: str
) -> None:
    mounted = tmp_path / "mounted.toml"
    mounted.write_text(f'[cdh.logs]\nmode="{mode}"\nmax_files=0\n')
    sources = read_runtime_config_sources(
        baked_config_path=tmp_path / "absent", mounted_config_path=mounted, environ={}
    )
    for admit in (load_runtime_log_settings, load_runtime_config_from_sources):
        with pytest.raises(RuntimeConfigurationError) as caught:
            admit(sources)
        diagnostic = caught.value.diagnostics[0]
        assert diagnostic.path == ("cdh", "logs", "max_files")
        assert isinstance(diagnostic.source_context, SourceLocation)
        assert diagnostic.source_context.source.label == str(mounted)


@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5"])
def test_invalid_environment_count_has_controlled_origin(
    tmp_path: Path, value: str
) -> None:
    sources = read_runtime_config_sources(
        baked_config_path=tmp_path / "absent",
        mounted_config_path=tmp_path / "missing",
        environ={"CDH_LOG_MAX_FILES": value},
    )
    with pytest.raises(RuntimeConfigurationError) as caught:
        load_runtime_log_settings(sources)
    assert caught.value.diagnostics[0].code == "env.invalid_log_max_files"
    assert isinstance(caught.value.diagnostics[0].source_context, SourceLocation)
