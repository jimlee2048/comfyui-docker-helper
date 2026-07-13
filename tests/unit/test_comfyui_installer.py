"""Exact detached ComfyUI staging and requirements verification contracts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from tests.unit.test_build_plan import accepted_resolution, final_config

from comfyui_docker_helper.comfyui_requirements import (
    CUDA_PROTECTED_REQUIREMENTS,
    parse_comfyui_requirements,
)
from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    build_plan_digest,
    construct_build_plan,
)
from comfyui_docker_helper.container import comfyui_installer
from comfyui_docker_helper.container.comfyui_installer import (
    ComfyUIInstallError,
    _checkout_exact,
    _rename_noreplace,
    _verify_checkout,
    _verify_floor_ancestry,
)
from comfyui_docker_helper.container.phase_inputs import phase_document
from comfyui_docker_helper.container.runners import ContainerRuntime

_REQUIREMENTS = b"torch\ntorchvision\ntorchaudio\nnumpy>=1.25\n"


@pytest.fixture(autouse=True)
def _fixture_checkout_has_supported_ancestry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(comfyui_installer, "_verify_floor_ancestry", lambda *_: None)


def _repository(path: Path) -> str:
    path.mkdir()
    (path / "main.py").write_text("print('ok')\n")
    (path / "requirements.txt").write_bytes(_REQUIREMENTS)
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
    plan = construct_build_plan(final_config(), accepted_resolution())
    document = plan.application.model_dump(mode="python")
    parsed = parse_comfyui_requirements(
        _REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        protected_names=CUDA_PROTECTED_REQUIREMENTS,
    )
    document["paths"]["workspace"] = str(workspace)
    document["paths"]["comfyui"] = str(target)
    document["comfyui"]["repository"] = str(source)
    document["comfyui"]["commit"] = commit
    document["comfyui"]["requirements"]["digest"] = parsed.digest
    application = ApplicationPhase.model_validate(document)
    runtime = ContainerRuntime(
        workspace=workspace, comfyui_path=target, virtual_env=Path("/opt/venv")
    )
    return application, runtime


def test_checkout_is_detached_exact_atomic_and_retains_git_metadata(
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
    )
    assert symbolic.returncode != 0
    assert not list(runtime.workspace.glob(".ComfyUI.stage-*"))


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


def test_checkout_wires_ancestry_after_identity_before_requirements_and_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
    events: list[str] = []

    def fake_run_git(argv, *, cwd, env, description):
        del cwd, env, description
        command = tuple(os.fspath(item) for item in argv)
        if "clone" in command:
            stage = Path(command[-1])
            (stage / "main.py").write_text("print('ok')\n")
            (stage / "requirements.txt").write_bytes(_REQUIREMENTS)
            audio = stage / "comfy_extras/nodes_audio.py"
            audio.parent.mkdir()
            audio.write_text("NODE_CLASS_MAPPINGS = {}\n")
            events.append("clone")
            return ""
        if "checkout" in command:
            events.append("checkout")
            return ""
        if "rev-parse" in command:
            events.append("head")
            return application.comfyui.commit
        if "get-url" in command:
            events.append("origin")
            return application.comfyui.repository
        raise AssertionError(command)

    monkeypatch.setattr(comfyui_installer, "_run_git", fake_run_git)
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_floor_ancestry",
        lambda *_args: events.append("ancestry"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_requirements",
        lambda *_args: events.append("requirements"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_rename_noreplace",
        lambda *_args: events.append("placement"),
    )

    _checkout_exact(application, runtime, Path("/usr/bin/git"), {})

    assert events == [
        "clone",
        "checkout",
        "head",
        "origin",
        "ancestry",
        "requirements",
        "placement",
    ]


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_checkout_rejects_every_preexisting_target_type(
    tmp_path: Path, kind: str
) -> None:
    application, runtime = _application(tmp_path)
    target = runtime.comfyui_path
    if kind == "file":
        target.write_text("occupied")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "missing")

    with pytest.raises(ComfyUIInstallError, match="already exists"):
        _checkout_exact(application, runtime, Path("/usr/bin/git"), {})


def test_checkout_failure_cleans_only_owned_stage(tmp_path: Path) -> None:
    application, runtime = _application(tmp_path)
    document = application.model_dump(mode="python")
    document["comfyui"]["commit"] = "f" * 40
    changed = ApplicationPhase.model_validate(document)
    sibling = runtime.workspace / "keep"
    sibling.write_text("keep")

    with pytest.raises(ComfyUIInstallError, match="checkout"):
        _checkout_exact(changed, runtime, Path("/usr/bin/git"), {})

    assert sibling.read_text() == "keep"
    assert not runtime.comfyui_path.exists()
    assert not list(runtime.workspace.glob(".ComfyUI.stage-*"))


def test_atomic_placement_never_replaces_a_racing_target(tmp_path: Path) -> None:
    source = tmp_path / "stage"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "source").write_text("source")
    (target / "owner").write_text("owner")

    with pytest.raises(ComfyUIInstallError, match="already exists"):
        _rename_noreplace(source, target)

    assert (source / "source").read_text() == "source"
    assert (target / "owner").read_text() == "owner"


def test_checkout_requirements_tamper_fails_expected_projection(tmp_path: Path) -> None:
    application, runtime = _application(tmp_path)
    _checkout_exact(application, runtime, Path("/usr/bin/git"), {})
    (runtime.comfyui_path / "requirements.txt").write_bytes(
        _REQUIREMENTS + b"torchcodec\n"
    )

    with pytest.raises(ComfyUIInstallError, match="canonical projection"):
        _verify_checkout(application, runtime)


def test_orchestration_verifies_checkout_before_any_package_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    digest = build_plan_digest(plan)
    application_path = tmp_path / "application.json"
    toolchain_path = tmp_path / "toolchain.json"
    application_path.write_text(
        phase_document("application", plan.application, digest).model_dump_json()
    )
    toolchain_path.write_text(
        phase_document("toolchain", plan.toolchain, digest).model_dump_json()
    )
    events: list[str] = []
    parsed = parse_comfyui_requirements(
        _REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        protected_names=CUDA_PROTECTED_REQUIREMENTS,
    )
    monkeypatch.setattr(
        comfyui_installer, "_checkout_exact", lambda *_args: events.append("checkout")
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_checkout",
        lambda *_args: events.append("verify") or parsed,
    )
    monkeypatch.setattr(
        comfyui_installer,
        "install_inference_group",
        lambda *_args, **_kwargs: events.append("inference"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_install_ordinary_requirements",
        lambda *_args: events.append("ordinary"),
    )
    runtime = ContainerRuntime(
        workspace=Path(plan.application.paths.workspace),
        comfyui_path=Path(plan.application.paths.comfyui),
        virtual_env=Path(plan.application.paths.venv),
    )

    comfyui_installer.install_comfyui(
        application_path,
        toolchain_path,
        expected_build_plan_digest=digest,
        runtime=runtime,
    )

    assert events == ["checkout", "verify", "inference", "ordinary"]


def test_ordinary_requirements_use_only_python_index_constraints_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
    constraints = tmp_path / "python-package-constraints.txt"
    constraints.write_text("torch==2.12.1+cu130\n")
    commands: list[tuple[str, ...]] = []
    temporary_paths: list[Path] = []
    captured_requirements: list[str] = []

    def fake_run_argv(argv, **_kwargs) -> None:
        command = tuple(os.fspath(item) for item in argv)
        commands.append(command)
        if "install" in command:
            path = Path(command[command.index("--requirements") + 1])
            temporary_paths.append(path)
            captured_requirements.append(path.read_text())
            assert path.stat().st_mode & 0o777 == 0o444

    monkeypatch.setattr(comfyui_installer, "_BUILD_DIRECTORY", tmp_path)
    monkeypatch.setattr(comfyui_installer, "run_argv", fake_run_argv)

    comfyui_installer._install_ordinary_requirements(
        application,
        ("numpy>=1.25", "requests"),
        runtime,
        Path("/usr/local/bin/uv"),
        constraints,
        {},
    )

    assert captured_requirements == ["numpy>=1.25\nrequests\n"]
    assert all(
        protected not in captured_requirements[0]
        for protected in ("torch", "torchvision", "torchaudio")
    )
    assert len(commands) == 2
    install, check = commands
    assert install[install.index("--default-index") + 1] == (
        application.python_index_url
    )
    assert install[install.index("--constraint") + 1] == os.fspath(constraints)
    assert "download.pytorch.org" not in " ".join(install)
    assert check[2:4] == ("pip", "check")
    assert not temporary_paths[0].exists()


def _git(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()
