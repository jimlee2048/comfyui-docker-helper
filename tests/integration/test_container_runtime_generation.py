"""Fresh runtime-generation admission coverage."""

from __future__ import annotations

from pathlib import Path

from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_serve import RuntimeGenerationFactory


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_hook(root: Path, filename: str) -> None:
    path = root / "pre-start.d" / filename
    _write(path, "#!/bin/sh\n")
    path.chmod(0o755)


def test_generation_factory_rereads_inputs_and_creates_fresh_owners(
    tmp_path: Path,
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

    factory = RuntimeGenerationFactory(
        runtime=runtime,
        baked_config_path=baked_config,
        mounted_config_path=mounted_config,
        baked_hooks_path=baked_hooks,
        mounted_hooks_path=mounted_hooks,
        environ=source_env,
        runtime_state_path=tmp_path / "state.json",
    )
    first = factory.create_generation()

    _write(
        baked_config,
        '[comfyui]\nlisten = "127.0.0.2"\n',
    )
    _write(mounted_config, "[comfyui]\nport = 8302\n")
    _write_hook(baked_hooks, "05-new-baked.sh")
    _write_hook(mounted_hooks, "15-new-mounted.sh")
    source_env["STATIC_VALUE"] = "changed"
    second = factory.create_generation()

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
    assert first.downloads is not second.downloads
    assert first.ssh_service is not second.ssh_service
