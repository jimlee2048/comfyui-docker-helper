"""Real Git checkout and ancestry evidence for the ComfyUI build owner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from tests.build_plan_support import accepted_resolution, build_plan, final_config

from comfyui_docker_helper.config.planning.build_plan import ApplicationPhase
from comfyui_docker_helper.config.planning.requirements import (
    CUDA_PROTECTED_REQUIREMENTS,
    parse_comfyui_requirements,
    parse_manager_requirements,
)
from comfyui_docker_helper.container.build import comfyui as comfyui_installer
from comfyui_docker_helper.container.build.comfyui import (
    ComfyUIInstallError,
    _checkout_exact,
    _verify_checkout,
    _verify_floor_ancestry,
)
from comfyui_docker_helper.container.process.runners import ContainerRuntime

_REQUIREMENTS = b"torch\ntorchvision\ntorchaudio\nnumpy>=1.25\n"
_MANAGER_REQUIREMENTS = b"comfyui_manager==4.0.5\n"
_LOCAL_GIT_TIMEOUT_SECONDS = 30


def _repository(path: Path) -> str:
    path.mkdir()
    (path / "main.py").write_text("print('ok')\n")
    (path / "requirements.txt").write_bytes(_REQUIREMENTS)
    (path / "manager_requirements.txt").write_bytes(_MANAGER_REQUIREMENTS)
    audio = path / "comfy_extras/nodes_audio.py"
    audio.parent.mkdir()
    audio.write_text("NODE_CLASS_MAPPINGS = {}\n")
    _git("init", cwd=path)
    _git("config", "user.email", "test@example.test", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    _git("add", "--all", cwd=path)
    _git("commit", "-m", "fixture", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


def _application(tmp_path: Path) -> tuple[ApplicationPhase, ContainerRuntime]:
    source = tmp_path / "source"
    commit = _repository(source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "ComfyUI"
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.application.model_dump(mode="python")
    parsed = parse_comfyui_requirements(
        _REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
        protected_names=CUDA_PROTECTED_REQUIREMENTS,
    )
    document["paths"]["workspace"] = str(workspace)
    document["paths"]["comfyui"] = str(target)
    paths = plan.application.paths.model_validate(document["paths"])
    requirements = plan.application.comfyui.requirements.model_copy(
        update={"digest": parsed.digest}
    )
    # The installer fixture substitutes a local source after BuildPlan admission.
    comfyui = plan.application.comfyui.model_copy(
        update={
            "repository": str(source),
            "commit": commit,
            "floor_commit": commit,
            "requirements": requirements,
        }
    )
    application = plan.application.model_copy(
        update={"paths": paths, "comfyui": comfyui}
    )
    runtime = ContainerRuntime(
        workspace=workspace, comfyui_path=target, virtual_env=Path("/opt/venv")
    )
    return application, runtime


# ComfyUI installation preserves exact checkout, source routing, and capability proofs.
def test_checkout_is_detached_exact_and_retains_git_metadata(
    tmp_path: Path,
) -> None:
    application, runtime = _application(tmp_path)

    _checkout_exact(application, runtime, Path("/usr/bin/git"), {})

    assert (runtime.comfyui_path / ".git").is_dir()
    assert _git("rev-parse", "HEAD", cwd=runtime.comfyui_path) == (
        application.comfyui.commit
    )
    symbolic = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=runtime.comfyui_path,
        check=False,
        timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
    )
    assert symbolic.returncode != 0


def test_floor_ancestry_accepts_descendant_and_rejects_older_or_unprovable(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "ancestry"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.test", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "owned").write_text("floor")
    _git("add", "--all", cwd=repository)
    _git("commit", "-m", "floor", cwd=repository)
    floor = _git("rev-parse", "HEAD", cwd=repository)
    (repository / "owned").write_text("descendant")
    _git("commit", "-am", "descendant", cwd=repository)
    descendant = _git("rev-parse", "HEAD", cwd=repository)

    _verify_floor_ancestry(
        repository, floor, descendant, Path("/usr/bin/git"), os.environ
    )
    with pytest.raises(ComfyUIInstallError, match="older than"):
        _verify_floor_ancestry(
            repository, descendant, floor, Path("/usr/bin/git"), os.environ
        )
    with pytest.raises(ComfyUIInstallError, match="could not be proven"):
        _verify_floor_ancestry(
            repository, "f" * 40, descendant, Path("/usr/bin/git"), os.environ
        )


def test_checkout_failure_preserves_unrelated_siblings(tmp_path: Path) -> None:
    application, runtime = _application(tmp_path)
    changed = application.model_copy(
        update={"comfyui": application.comfyui.model_copy(update={"commit": "f" * 40})}
    )
    sibling = runtime.workspace / "keep"
    sibling.write_text("keep")

    with pytest.raises(ComfyUIInstallError, match="checkout"):
        _checkout_exact(changed, runtime, Path("/usr/bin/git"), {})

    assert sibling.read_text() == "keep"
    assert runtime.comfyui_path.is_dir()


# Checkout and requirements proofs complete before any application package mutation.
def test_checkout_requirements_tamper_fails_expected_projection(tmp_path: Path) -> None:
    application, runtime = _application(tmp_path)
    _checkout_exact(application, runtime, Path("/usr/bin/git"), {})
    (runtime.comfyui_path / "requirements.txt").write_bytes(
        _REQUIREMENTS + b"torchcodec\n"
    )

    with pytest.raises(ComfyUIInstallError, match="canonical projection"):
        _verify_checkout(application, runtime)


def test_manager_requirements_are_verified_before_install_and_use_python_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
    _checkout_exact(application, runtime, Path("/usr/bin/git"), {})
    manager = application.comfyui.manager
    assert manager is not None
    parsed = parse_manager_requirements(
        _MANAGER_REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
    )
    constraints = tmp_path / "python-package-constraints.txt"
    constraints.write_text("torch==2.12.1+cu130\n")
    commands: list[tuple[tuple[str, ...], dict]] = []
    requirements_paths: list[Path] = []
    events: list[str] = []

    def fake_run_argv(argv, **kwargs) -> None:
        command = tuple(os.fspath(item) for item in argv)
        commands.append((command, kwargs))
        if "install" in command:
            path = Path(command[command.index("--requirements") + 1])
            requirements_paths.append(path)
            assert path.read_text() == "comfyui_manager==4.0.5\n"
            assert path.name.startswith("manager-requirements-")
            assert path.stat().st_mode & 0o077 == 0

    monkeypatch.setattr(comfyui_installer, "_BUILD_DIRECTORY", tmp_path)
    monkeypatch.setattr(comfyui_installer, "run_argv", fake_run_argv)
    monkeypatch.setattr(
        comfyui_installer,
        "_write_import_anchor",
        lambda path, workspace: events.append(f"anchor:{path}:{workspace}"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_manager_import_root",
        lambda *_args: events.append("import root"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_declared_manager_distributions",
        lambda *_args: events.append("declared distributions"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_manager_import_anchor",
        lambda _application, observed_manager, _runtime: (
            events.append("anchor proof") or Path(observed_manager.import_anchor).parent
        ),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_cm_cli",
        lambda path, observed_runtime: events.append(
            f"cm-cli:{path}:{observed_runtime.python}"
        ),
    )

    comfyui_installer._install_manager_capability(
        application,
        manager,
        parsed,
        runtime,
        Path("/usr/local/bin/uv"),
        constraints,
        {
            "PATH": "/poison/bin",
            "LIBRARY_PATH": "/usr/local/cuda/lib64/stubs",
            "CUDA_HOME": "/usr/local/cuda",
            "PIP_INDEX_URL": "https://poison.example/simple",
        },
    )

    assert len(commands) == 1
    install, install_kwargs = commands[0]
    assert install[install.index("--default-index") + 1] == (
        application.python_index_url
    )
    assert install[install.index("--constraint") + 1] == os.fspath(constraints)
    assert "download.pytorch.org" not in " ".join(install)
    assert install_kwargs["env"]["UV_CONSTRAINT"] == os.fspath(constraints)
    assert install_kwargs["env"]["PIP_CONSTRAINT"] == os.fspath(constraints)
    assert install_kwargs["env"]["PATH"] == "/usr/local/cuda/bin:/usr/bin:/bin"
    assert install_kwargs["env"]["LIBRARY_PATH"] == "/usr/local/cuda/lib64/stubs"
    assert install_kwargs["env"]["CUDA_HOME"] == "/usr/local/cuda"
    assert "PIP_INDEX_URL" not in install_kwargs["env"]
    assert events == [
        f"anchor:{manager.import_anchor}:{runtime.comfyui_path}",
        "import root",
        "anchor proof",
        "declared distributions",
        f"cm-cli:{manager.executable}:{runtime.python}",
    ]
    assert not requirements_paths[0].exists()


@pytest.mark.parametrize(
    "kind, message",
    [
        ("missing", "could not be read"),
        ("directory", "regular file"),
        ("source", "changes package sources"),
        ("direct", "uses a direct source"),
    ],
)
def test_manager_requirements_fail_closed_before_package_mutation(
    tmp_path: Path, kind: str, message: str
) -> None:
    application, runtime = _application(tmp_path)
    _checkout_exact(application, runtime, Path("/usr/bin/git"), {})
    manager = application.comfyui.manager
    assert manager is not None
    path = runtime.comfyui_path / manager.requirements_path
    path.unlink()
    if kind == "directory":
        path.mkdir()
    elif kind == "source":
        path.write_text("--index-url https://poison.test/simple\n")
    elif kind == "direct":
        path.write_text("comfyui_manager @ https://poison.test/manager.whl\n")

    with pytest.raises(ComfyUIInstallError, match=message):
        _verify_checkout(application, runtime)


def _git(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
    )
    return completed.stdout.strip()
