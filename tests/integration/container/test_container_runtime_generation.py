"""Fresh runtime-generation admission coverage."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_secret_session import (
    RuntimeDownloaderCredentialPolicy,
)
from comfyui_docker_helper.container.runtime_serve import (
    RuntimeGenerationFactory,
    capture_runtime_environment,
)
from tests.runtime_event_support import RecordingRuntimeEventSink


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_hook(root: Path, filename: str) -> None:
    path = root / "pre-start.d" / filename
    _write(path, "#!/bin/sh\n")
    path.chmod(0o755)


def test_generation_factory_rereads_inputs_and_creates_fresh_owners(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )
    baked_config = tmp_path / "baked.toml"
    mounted_config = tmp_path / "mounted.toml"
    baked_hooks = tmp_path / "baked-hooks"
    mounted_hooks = tmp_path / "mounted-hooks"
    source_env = {"STATIC_VALUE": "captured"}
    _write(
        baked_config,
        '[comfyui]\nlisten = "0.0.0.0"\n',
    )
    _write(mounted_config, "[comfyui]\nport = 8201\n")
    _write_hook(baked_hooks, "10-baked.sh")
    _write_hook(mounted_hooks, "20-mounted.sh")
    _write(mounted_hooks / "README.md", "ordinary deployment notes\n")

    runtime_events = RecordingRuntimeEventSink()
    factory = RuntimeGenerationFactory(
        runtime=runtime,
        background_event_sink=runtime_events,
        event_sink=runtime_events,
        baked_config_path=baked_config,
        mounted_config_path=mounted_config,
        baked_hooks_path=baked_hooks,
        mounted_hooks_path=mounted_hooks,
        environment=capture_runtime_environment(source_env),
        runtime_state_path=tmp_path / "state.json",
    )
    first = factory.create_generation()
    first_diagnostics = capsys.readouterr()

    _write(
        baked_config,
        '[comfyui]\nlisten = "127.0.0.2"\n',
    )
    _write(mounted_config, "[comfyui]\nport = 8302\n")
    _write_hook(baked_hooks, "05-new-baked.sh")
    _write_hook(mounted_hooks, "15-new-mounted.sh")
    source_env["STATIC_VALUE"] = "changed"
    second = factory.create_generation()
    second_diagnostics = capsys.readouterr()

    assert (first.config.comfyui.listen, first.config.comfyui.port) == (
        "0.0.0.0",
        8201,
    )
    assert (second.config.comfyui.listen, second.config.comfyui.port) == (
        "127.0.0.2",
        8302,
    )
    assert [(hook.source, hook.filename) for hook in first.hook_plan.hooks] == [
        ("baked", "10-baked.sh"),
        ("mounted", "20-mounted.sh"),
    ]
    assert [(hook.source, hook.filename) for hook in second.hook_plan.hooks] == [
        ("baked", "05-new-baked.sh"),
        ("baked", "10-baked.sh"),
        ("mounted", "15-new-mounted.sh"),
        ("mounted", "20-mounted.sh"),
    ]
    assert first.source_env["STATIC_VALUE"] == "captured"
    assert second.source_env["STATIC_VALUE"] == "captured"
    assert first.source_env is second.source_env
    assert first.source_env_bytes is second.source_env_bytes
    assert first.source_env_bytes[b"STATIC_VALUE"] == b"captured"
    assert first.environment is second.environment
    assert "captured" not in repr(first.environment)
    assert "captured" not in repr(first)
    assert first.downloads is not second.downloads
    assert first.ssh_service is not second.ssh_service
    assert first_diagnostics.err.count("Runtime hook warnings:") == 1
    assert second_diagnostics.err.count("Runtime hook warnings:") == 1
    assert "ignored 1 ordinary top-level runtime hook" in first_diagnostics.err
    assert "ignored 1 ordinary top-level runtime hook" in second_diagnostics.err


def test_generation_factory_wires_one_fresh_credential_policy_per_generation(
    tmp_path: Path,
) -> None:
    runtime = ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )
    runtime.comfyui_path.mkdir(parents=True)
    config = tmp_path / "runtime.toml"
    _write(
        config,
        """
[cdh]
default_downloader = "httpx"

[[cdh.downloader.credentials]]
match = "https://example.test/private/"
type = "bearer"
token = { secret = "runtime_read" }

[secrets.runtime_read]
env = "RUNTIME_TOKEN"

[[files]]
type = "http"
url = "https://example.test/private/sync.bin"
target_dir = "models"
filename = "sync.bin"
download_mode = "sync"

[[files]]
type = "http"
url = "https://example.test/private/async.bin"
target_dir = "models"
filename = "async.bin"
download_mode = "async"
""",
    )
    sync_policies: list[RuntimeDownloaderCredentialPolicy] = []
    async_policies: list[RuntimeDownloaderCredentialPolicy] = []

    def downloader(_plan, **kwargs: object):
        policy = kwargs["credential_policy"]
        assert isinstance(policy, RuntimeDownloaderCredentialPolicy)
        sync_policies.append(policy)
        return ()

    class AcceptedQueue:
        pass

    def async_starter(_plan, **kwargs: object):
        policy = kwargs["credential_policy"]
        handle_observer = kwargs["handle_observer"]
        assert isinstance(policy, RuntimeDownloaderCredentialPolicy)
        assert callable(handle_observer)
        async_policies.append(policy)
        handle = AcceptedQueue()
        handle_observer(handle)
        return handle

    runtime_events = RecordingRuntimeEventSink()
    factory = RuntimeGenerationFactory(
        runtime=runtime,
        background_event_sink=runtime_events,
        event_sink=runtime_events,
        baked_config_path=config,
        mounted_config_path=tmp_path / "missing-mounted.toml",
        baked_hooks_path=tmp_path / "missing-baked-hooks",
        mounted_hooks_path=tmp_path / "missing-mounted-hooks",
        environment=capture_runtime_environment(
            {"RUNTIME_TOKEN": "runtime-test-token"}
        ),
        runtime_downloader=downloader,
        runtime_async_queue_starter=async_starter,
        runtime_state_path=tmp_path / "state.json",
    )

    for generation in (factory.create_generation(), factory.create_generation()):
        generation.downloads.activate()
        generation.downloads.start_async(cancel_requested=lambda: False)

    assert sync_policies[0] is async_policies[0]
    assert sync_policies[1] is async_policies[1]
    assert sync_policies[0] is not sync_policies[1]
    assert sync_policies[0].session is not sync_policies[1].session
    assert [
        policy.authorization_for(httpx.URL("https://example.test/private/model.bin"))
        for policy in sync_policies
    ] == [b"Bearer runtime-test-token", b"Bearer runtime-test-token"]


def test_runtime_environment_capture_preserves_production_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = b"CDH_RAW_ENVIRONMENT_TEST"
    value = b"raw-\xff-value"
    monkeypatch.setitem(os.environb, name, value)

    snapshot = capture_runtime_environment()

    assert snapshot.raw[name] == value
    assert os.fsencode(snapshot.text[os.fsdecode(name)]) == value
    assert "raw-" not in repr(snapshot)


def test_runtime_environment_capture_derives_immutable_bytes_from_text_seam() -> None:
    raw_name = b"RAW_\xff_NAME"
    raw_value = b"captured-\xfe-value"
    text_name = os.fsdecode(raw_name)
    text_value = os.fsdecode(raw_value)
    source_env = {text_name: text_value}

    snapshot = capture_runtime_environment(source_env)
    source_env[text_name] = "changed"

    assert snapshot.text[text_name] == text_value
    assert snapshot.raw[raw_name] == raw_value
    with pytest.raises(TypeError):
        snapshot.text[text_name] = "mutation"  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.raw[raw_name] = b"mutation"  # type: ignore[index]
