"""Local-node mounted input, copy, execution, and final-root boundaries."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from tests.container_installer_support import (
    application,
    custom_nodes_phase,
    local_node,
    patch_phases,
)

from comfyui_docker_helper.config.planning.build_plan import HookPlan
from comfyui_docker_helper.container.build.custom_nodes import (
    local,
    orchestrator,
    root_install,
)
from comfyui_docker_helper.container.build.custom_nodes.contracts import (
    CustomNodeInstallError,
)
from comfyui_docker_helper.container.process.runners import ContainerRuntime, run_argv


def _mount(source: Path, node) -> Path:
    mounted = source.parent / "mounted"
    mounted.mkdir()
    source.rename(mounted / Path(node.context_path).name)
    return mounted


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_local_copy_consumes_files_once_and_applies_image_modes(
    tmp_path, monkeypatch, locked
):
    _app, runtime = application(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "empty").mkdir()
    (source / "data").mkdir()
    (source / "data/content").write_bytes(b"payload" * 200_000)
    (source / "data/content").chmod(0o755)
    (source / ".dockerignore").write_text("*\n")
    node = local_node(runtime, source, locked=locked)
    mounted = _mount(source, node)
    consumed = []
    consume = local.consume_regular_absolute_file

    def record(path, sink):
        consumed.append(
            path.relative_to(mounted / Path(node.context_path).name).as_posix()
        )
        return consume(path, sink)

    monkeypatch.setattr(local, "consume_regular_absolute_file", record)
    if not locked:
        monkeypatch.setattr(
            local.hashlib,
            "sha256",
            lambda *_args: pytest.fail("unlocked copy must not hash"),
        )
    target = local.prepare_local_node(
        node, runtime.comfyui_path / "custom_nodes", local_nodes_directory=mounted
    )
    assert consumed == [".dockerignore", "data/content"]
    assert (target / "data/content").read_bytes() == b"payload" * 200_000
    for path in (target, target / "empty", target / "data"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
    for path in (target / ".dockerignore", target / "data/content"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "missing",
        "file-link",
        "directory-link",
        "special",
        "bytes",
        "size",
        "aggregate",
    ],
)
def test_local_copy_rejects_unproved_input(tmp_path, mutation):
    _app, runtime = application(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "content").write_text("original")
    (source / "directory").mkdir()
    node = local_node(runtime, source, locked=True)
    if mutation == "extra":
        (source / "injected").write_text("extra")
    elif mutation == "missing":
        (source / "content").unlink()
    elif mutation == "file-link":
        (source / "content").unlink()
        (source / "content").symlink_to(tmp_path / "outside")
    elif mutation == "directory-link":
        (source / "directory").rmdir()
        (source / "directory").symlink_to(tmp_path, target_is_directory=True)
    elif mutation == "special":
        (source / "content").unlink()
        os.mkfifo(source / "content")
    elif mutation == "bytes":
        (source / "content").write_text("tampered")
    elif mutation == "size":
        (source / "content").write_text("longer than original")
    else:
        node = node.model_copy(update={"tree_digest": f"sha256:{'0' * 64}"})
    mounted = _mount(source, node)
    with pytest.raises(CustomNodeInstallError):
        local.prepare_local_node(
            node, runtime.comfyui_path / "custom_nodes", local_nodes_directory=mounted
        )


@pytest.mark.parametrize("shape", ["directory", "file", "link"])
def test_local_target_creation_is_exclusive(tmp_path, shape):
    _app, runtime = application(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    node = local_node(runtime, source)
    mounted = _mount(source, node)
    target = Path(node.target)
    if shape == "directory":
        target.mkdir()
    elif shape == "file":
        target.write_text("existing")
    else:
        target.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CustomNodeInstallError, match="copy failed"):
        local.prepare_local_node(
            node, runtime.comfyui_path / "custom_nodes", local_nodes_directory=mounted
        )


@pytest.mark.parametrize("surfaces", ["both", "created", "deleted", "empty"])
def test_local_real_hooks_control_root_installation_and_allow_final_patches(
    tmp_path, monkeypatch, surfaces
):
    app, initial = application(tmp_path)
    runtime = ContainerRuntime(
        workspace=initial.workspace,
        comfyui_path=initial.comfyui_path,
        virtual_env=Path(sys.prefix),
    )
    app = app.model_copy(
        update={
            "paths": app.paths.model_copy(update={"venv": str(sys.prefix)}),
            "comfyui": app.comfyui.model_copy(update={"manager": None}),
        }
    )
    source = tmp_path / "source"
    source.mkdir()
    if surfaces in {"both", "deleted"}:
        (source / "requirements.txt").write_text("unpatched==1\n")
        (source / "install.py").write_text("raise RuntimeError('unpatched')\n")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    install_script = (
        "from pathlib import Path\np = Path.cwd()\n"
        "assert p.name == 'local-node'\n"
        "assert (p / 'requirements-consumed').exists()\n"
        "(p / 'installed').write_text('yes')\n"
        "(p / 'install.py').unlink()\n"
    )
    common = (
        "import os\nfrom pathlib import Path\n"
        "root = Path(os.environ['COMFYUI_PATH'])\n"
        "assert Path.cwd() == root\n"
        "target = root / 'custom_nodes/local-node'\nassert target.is_dir()\n"
    )
    pre = common
    if surfaces in {"both", "created"}:
        pre += (
            "(target / 'requirements.txt').write_text('packaging==24.0\\n')\n"
            f"(target / 'install.py').write_text({install_script!r})\n"
        )
    elif surfaces == "deleted":
        pre += (
            "(target / 'requirements.txt').unlink()\n(target / 'install.py').unlink()\n"
        )
    pre += "(root / 'pre-ran').touch()\n"
    post = common + "(root / 'post-ran').touch()\n"

    def hook(name, text):
        data = text.encode()
        (hooks / name).write_bytes(data)
        return HookPlan(
            relative_path=name, digest=f"sha256:{hashlib.sha256(data).hexdigest()}"
        )

    node = local_node(
        runtime,
        source,
        locked=True,
        pre=(hook("pre.py", pre),),
        post=(hook("post.py", post),),
    )
    mounted = _mount(source, node)
    phase = custom_nodes_phase(runtime, (node,), install_manager=False)
    patch_phases(monkeypatch, app, phase)
    prepare = local.prepare_local_node
    monkeypatch.setattr(
        local,
        "prepare_local_node",
        lambda node, root: prepare(node, root, local_nodes_directory=mounted),
    )
    requirements = []

    def install(argv, **kwargs):
        if "--requirements" in argv:
            path = Path(argv[argv.index("--requirements") + 1])
            assert kwargs["cwd"] == Path(node.target)
            assert kwargs["env"]["UV_DEFAULT_INDEX"] == app.python_index_url
            assert kwargs["env"]["UV_INDEX"] == app.pytorch.pytorch_index_url
            assert kwargs["env"]["UV_CONSTRAINT"]
            requirements.append(path.read_text())
            (path.parent / "requirements-consumed").touch()
            return subprocess.CompletedProcess(argv, 0)
        return run_argv(argv, **kwargs)

    monkeypatch.setattr(root_install, "run_argv", install)
    orchestrator.install_custom_nodes(
        phase, app, runtime=runtime, build_hooks_directory=hooks
    )
    assert (runtime.comfyui_path / "pre-ran").exists()
    assert (runtime.comfyui_path / "post-ran").exists()
    assert requirements == (
        ["packaging==24.0\n"] if surfaces in {"both", "created"} else []
    )
    # Later authoritative files can replace original bytes or add arbitrary content.
    target = Path(node.target)
    (target / "requirements.txt").write_text("later files overlay\n")
    (target / "arbitrary-link").symlink_to(tmp_path / "outside")
    inventory = orchestrator.observe_custom_node_state(phase, runtime=runtime)
    assert inventory.nodes[0].model_dump() == {
        "type": "local",
        "target": "local-node",
        "verification": "local-directory",
        "control": "direct-local",
    }
    target.rename(target.with_name("moved"))
    target.symlink_to(target.with_name("moved"), target_is_directory=True)
    with pytest.raises(CustomNodeInstallError, match="real directory"):
        orchestrator.observe_custom_node_state(phase, runtime=runtime)
