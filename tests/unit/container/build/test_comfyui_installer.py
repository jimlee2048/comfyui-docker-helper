"""Exact detached ComfyUI checkout and requirements verification contracts."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

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
    observe_application_state,
)
from comfyui_docker_helper.container.build.events import (
    ComfyUIInstallCompleted,
    ContainerHelperEvent,
    ContainerHelperPhase,
    ContainerHelperPhaseCompleted,
    ContainerHelperPhaseStarted,
)
from comfyui_docker_helper.container.process.runners import (
    ContainerCommandError,
    ContainerRuntime,
)

_REQUIREMENTS = b"torch\ntorchvision\ntorchaudio\nnumpy>=1.25\n"
_MANAGER_REQUIREMENTS = b"comfyui_manager==4.0.5\n"


def _helper_event_signature(event: object) -> object:
    if isinstance(event, ContainerHelperPhaseStarted):
        return ("phase-started", event.phase)
    if isinstance(event, ContainerHelperPhaseCompleted):
        return ("phase-completed", event.phase)
    if isinstance(event, ComfyUIInstallCompleted):
        return ("install-completed",)
    return event


@pytest.fixture(autouse=True)
def _fixture_checkout_has_supported_ancestry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(comfyui_installer, "_verify_floor_ancestry", lambda *_: None)


def _application(tmp_path: Path) -> tuple[ApplicationPhase, ContainerRuntime]:
    source = tmp_path / "source"
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
    # Unit fixtures provide a synthetic local source after BuildPlan admission.
    comfyui = plan.application.comfyui.model_copy(
        update={
            "repository": str(source),
            "commit": "1" * 40,
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


def _local_manager_application(
    tmp_path: Path,
) -> tuple[ApplicationPhase, ContainerRuntime, Path]:
    application, runtime = _application(tmp_path)
    virtual_env = tmp_path / "venv"
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    anchor = (
        virtual_env
        / "lib"
        / f"python{python_minor}"
        / "site-packages"
        / "comfyui-docker-helper-comfyui.pth"
    )
    manager = application.comfyui.manager
    assert manager is not None
    application = application.model_copy(
        update={
            "paths": application.paths.model_copy(update={"venv": str(virtual_env)}),
            "comfyui": application.comfyui.model_copy(
                update={
                    "manager": manager.model_copy(update={"import_anchor": str(anchor)})
                }
            ),
        }
    )
    runtime = ContainerRuntime(
        workspace=runtime.workspace,
        comfyui_path=runtime.comfyui_path,
        virtual_env=virtual_env,
    )
    anchor.parent.mkdir(parents=True)
    return application, runtime, anchor


# ComfyUI installation preserves exact checkout, source routing, and capability proofs.


def test_checkout_wires_final_target_and_proofs_before_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
    events: list[str] = []

    def fake_run_git(argv, *, cwd, env, description):
        del cwd, description
        assert env == {
            "HTTPS_PROXY": "https://proxy.example",
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        command = tuple(os.fspath(item) for item in argv)
        if "clone" in command:
            checkout = Path(command[-1])
            assert checkout == runtime.comfyui_path
            assert checkout.is_dir()
            assert not tuple(checkout.iterdir())
            (checkout / "main.py").write_text("print('ok')\n")
            (checkout / "requirements.txt").write_bytes(_REQUIREMENTS)
            (checkout / "manager_requirements.txt").write_bytes(_MANAGER_REQUIREMENTS)
            audio = checkout / "comfy_extras/nodes_audio.py"
            audio.parent.mkdir()
            audio.write_text("NODE_CLASS_MAPPINGS = {}\n")
            events.append("clone")
            return ""
        if "checkout" in command:
            events.append("checkout")
            return ""
        if "rev-parse" in command:
            if "--abbrev-ref" in command:
                events.append("detached")
                return "HEAD"
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
        "_read_manager_requirements",
        lambda *_args: events.append("manager requirements"),
    )

    _checkout_exact(
        application,
        runtime,
        Path("/usr/bin/git"),
        {
            "HTTPS_PROXY": "https://proxy.example",
            "LIBRARY_PATH": "/usr/local/cuda/lib64/stubs",
            "CUDA_HOME": "/usr/local/cuda",
        },
    )

    assert events == [
        "clone",
        "checkout",
        "head",
        "detached",
        "origin",
        "ancestry",
        "requirements",
        "manager requirements",
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
        missing = tmp_path / "missing"
        target.symlink_to(missing)

    with pytest.raises(ComfyUIInstallError, match="already exists"):
        _checkout_exact(application, runtime, Path("/usr/bin/git"), {})

    if kind == "file":
        assert target.read_text() == "occupied"
    elif kind == "directory":
        assert target.is_dir()
        assert not tuple(target.iterdir())
    else:
        assert target.is_symlink()
        assert target.readlink() == missing


# Checkout and requirements proofs complete before any application package mutation.


def test_orchestration_verifies_checkout_before_any_package_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    events: list[str | ContainerHelperEvent] = []
    parsed = parse_comfyui_requirements(
        _REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
        protected_names=CUDA_PROTECTED_REQUIREMENTS,
    )
    parsed_manager = parse_manager_requirements(
        _MANAGER_REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
    )
    monkeypatch.setattr(
        comfyui_installer, "_checkout_exact", lambda *_args: events.append("checkout")
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_checkout",
        lambda *_args: events.append("verify") or (parsed, parsed_manager),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "install_inference_group",
        lambda *_args, **_kwargs: events.append("inference"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "install_python_extras",
        lambda *_args, **_kwargs: events.append("extras"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_install_ordinary_requirements",
        lambda *_args: events.append("ordinary"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_install_manager_capability",
        lambda *_args: events.append("manager"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "verify_application_environment",
        lambda *_args, **_kwargs: events.append("health"),
    )
    runtime = ContainerRuntime(
        workspace=Path(plan.application.paths.workspace),
        comfyui_path=Path(plan.application.paths.comfyui),
        virtual_env=Path(plan.application.paths.venv),
    )

    comfyui_installer.install_comfyui(
        plan.application,
        plan.toolchain,
        runtime=runtime,
        event_sink=SimpleNamespace(emit=events.append),
    )

    assert [_helper_event_signature(event) for event in events] == [
        ("phase-started", ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT),
        "checkout",
        ("phase-completed", ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT),
        ("phase-started", ContainerHelperPhase.COMFYUI_SOURCE_VERIFICATION),
        "verify",
        ("phase-completed", ContainerHelperPhase.COMFYUI_SOURCE_VERIFICATION),
        ("phase-started", ContainerHelperPhase.PYTORCH_INSTALLATION),
        "inference",
        ("phase-completed", ContainerHelperPhase.PYTORCH_INSTALLATION),
        ("phase-started", ContainerHelperPhase.PYTHON_EXTRAS_INSTALLATION),
        "extras",
        "health",
        ("phase-completed", ContainerHelperPhase.PYTHON_EXTRAS_INSTALLATION),
        (
            "phase-started",
            ContainerHelperPhase.COMFYUI_REQUIREMENTS_INSTALLATION,
        ),
        "ordinary",
        "health",
        (
            "phase-completed",
            ContainerHelperPhase.COMFYUI_REQUIREMENTS_INSTALLATION,
        ),
        ("phase-started", ContainerHelperPhase.MANAGER_INSTALLATION),
        "manager",
        ("phase-completed", ContainerHelperPhase.MANAGER_INSTALLATION),
        ("phase-started", ContainerHelperPhase.COMFYUI_FINAL_VERIFICATION),
        "health",
        ("phase-completed", ContainerHelperPhase.COMFYUI_FINAL_VERIFICATION),
        ("install-completed",),
    ]


def test_orchestration_skips_disabled_optional_phases_and_checks_manager_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.application.model_dump(mode="python")
    document["comfyui"]["manager"] = None
    assert document["python_extras"] is not None
    document["python_extras"]["packages"] = ()
    application = ApplicationPhase.model_validate(document)
    parsed = parse_comfyui_requirements(
        b"torch\ntorchvision\ntorchaudio\n",
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
        protected_names=CUDA_PROTECTED_REQUIREMENTS,
    )
    assert not parsed.ordinary
    events: list[str | ContainerHelperEvent] = []
    monkeypatch.setattr(
        comfyui_installer, "_checkout_exact", lambda *_args: events.append("checkout")
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_checkout",
        lambda *_args: events.append("verify") or (parsed, None),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "install_inference_group",
        lambda *_args, **_kwargs: events.append("inference"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "install_python_extras",
        lambda *_args, **_kwargs: events.append("extras"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_install_ordinary_requirements",
        lambda *_args: events.append("ordinary"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_manager_absent",
        lambda *_args: events.append("manager absent"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "verify_application_environment",
        lambda *_args, **_kwargs: events.append("health"),
    )
    runtime = ContainerRuntime(
        workspace=Path(application.paths.workspace),
        comfyui_path=Path(application.paths.comfyui),
        virtual_env=Path(application.paths.venv),
    )

    comfyui_installer.install_comfyui(
        application,
        plan.toolchain,
        runtime=runtime,
        event_sink=SimpleNamespace(emit=events.append),
    )

    assert [_helper_event_signature(event) for event in events] == [
        ("phase-started", ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT),
        "checkout",
        ("phase-completed", ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT),
        ("phase-started", ContainerHelperPhase.COMFYUI_SOURCE_VERIFICATION),
        "verify",
        ("phase-completed", ContainerHelperPhase.COMFYUI_SOURCE_VERIFICATION),
        ("phase-started", ContainerHelperPhase.PYTORCH_INSTALLATION),
        "inference",
        "extras",
        "health",
        ("phase-completed", ContainerHelperPhase.PYTORCH_INSTALLATION),
        "ordinary",
        "health",
        "manager absent",
        ("phase-started", ContainerHelperPhase.COMFYUI_FINAL_VERIFICATION),
        "health",
        ("phase-completed", ContainerHelperPhase.COMFYUI_FINAL_VERIFICATION),
        ("install-completed",),
    ]


def test_orchestration_failure_does_not_complete_active_phase_or_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    parsed = parse_comfyui_requirements(
        _REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
        protected_names=CUDA_PROTECTED_REQUIREMENTS,
    )
    events: list[ContainerHelperEvent] = []
    failure = ComfyUIInstallError("inference install failed")
    monkeypatch.setattr(comfyui_installer, "_checkout_exact", lambda *_args: None)
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_checkout",
        lambda *_args: (parsed, None),
    )

    def fail_inference(*_args, **_kwargs) -> None:
        raise failure

    monkeypatch.setattr(
        comfyui_installer,
        "install_inference_group",
        fail_inference,
    )
    runtime = ContainerRuntime(
        workspace=Path(plan.application.paths.workspace),
        comfyui_path=Path(plan.application.paths.comfyui),
        virtual_env=Path(plan.application.paths.venv),
    )

    with pytest.raises(ComfyUIInstallError) as raised:
        comfyui_installer.install_comfyui(
            plan.application,
            plan.toolchain,
            runtime=runtime,
            event_sink=SimpleNamespace(emit=events.append),
        )

    assert raised.value is failure
    assert events[-1] == ContainerHelperPhaseStarted(
        ContainerHelperPhase.PYTORCH_INSTALLATION
    )
    assert (
        ContainerHelperPhaseCompleted(ContainerHelperPhase.PYTORCH_INSTALLATION)
        not in events
    )
    assert ComfyUIInstallCompleted() not in events


def test_disabled_manager_state_rejects_installed_distribution_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.application.model_dump(mode="python")
    document["comfyui"]["manager"] = None
    application = ApplicationPhase.model_validate(document)
    runtime = ContainerRuntime()
    monkeypatch.setattr(
        comfyui_installer.importlib_metadata,
        "distributions",
        lambda **_kwargs: (SimpleNamespace(metadata={"Name": "ComfyUI_Manager"}),),
    )

    with pytest.raises(ComfyUIInstallError, match="distribution exists"):
        comfyui_installer._verify_manager_absent(application, runtime)


def test_disabled_manager_state_rejects_import_root_without_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime, anchor = _local_manager_application(tmp_path)
    application = application.model_copy(
        update={"comfyui": application.comfyui.model_copy(update={"manager": None})}
    )
    (anchor.parent / "comfyui_manager").mkdir()
    monkeypatch.setattr(
        comfyui_installer.importlib_metadata,
        "distributions",
        lambda **_kwargs: (),
    )

    with pytest.raises(ComfyUIInstallError, match="import root exists"):
        comfyui_installer._verify_manager_absent(application, runtime)


def test_application_observation_rechecks_source_input_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
    parsed = parse_comfyui_requirements(
        _REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
        protected_names=CUDA_PROTECTED_REQUIREMENTS,
    )
    events: list[object] = []

    def verify_checkout_identity(*args) -> None:
        assert args[3] == {
            "HTTPS_PROXY": "https://proxy.example",
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        events.append("source")

    monkeypatch.setattr(
        comfyui_installer,
        "_verify_checkout_identity",
        verify_checkout_identity,
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_requirements",
        lambda *_args: events.append("requirements") or parsed,
    )
    monkeypatch.setattr(
        comfyui_installer,
        "verify_application_environment",
        lambda *_args, **kwargs: events.append(
            (
                "application",
                kwargs["ordinary_requirements"],
            )
        ),
    )

    observe_application_state(
        application,
        runtime,
        parsed,
        environ={
            "HTTPS_PROXY": "https://proxy.example",
            "LIBRARY_PATH": "/usr/local/cuda/lib64/stubs",
            "CUDA_HOME": "/usr/local/cuda",
        },
    )

    assert events == [
        "source",
        "requirements",
        ("application", parsed.ordinary),
    ]


# Ordinary and Manager requirements stay on the Python source with exact constraints.
def test_ordinary_requirements_use_only_python_index_constraints_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
    constraints = tmp_path / "python-package-constraints.txt"
    constraints.write_text("torch==2.12.1+cu130\n")
    commands: list[tuple[str, ...]] = []
    temporary_paths: list[Path] = []
    captured_requirements: list[str] = []

    def fake_run_argv(argv, **kwargs) -> None:
        command = tuple(os.fspath(item) for item in argv)
        commands.append(command)
        if "install" in command:
            assert kwargs["env"]["PATH"] == "/usr/local/cuda/bin:/usr/bin:/bin"
            assert kwargs["env"]["LIBRARY_PATH"] == "/usr/local/cuda/lib64/stubs"
            assert kwargs["env"]["CUDA_HOME"] == "/usr/local/cuda"
            assert kwargs["env"]["PIP_CONSTRAINT"] == os.fspath(constraints)
            assert kwargs["env"]["UV_CONSTRAINT"] == os.fspath(constraints)
            assert "PIP_INDEX_URL" not in kwargs["env"]
            path = Path(command[command.index("--requirements") + 1])
            temporary_paths.append(path)
            captured_requirements.append(path.read_text())
            assert path.name.startswith("comfyui-requirements-")
            assert path.stat().st_mode & 0o077 == 0

    monkeypatch.setattr(comfyui_installer, "_BUILD_DIRECTORY", tmp_path)
    monkeypatch.setattr(comfyui_installer, "run_argv", fake_run_argv)

    comfyui_installer._install_ordinary_requirements(
        application,
        ("numpy>=1.25", "requests"),
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

    assert captured_requirements == ["numpy>=1.25\nrequests\n"]
    assert all(
        protected not in captured_requirements[0]
        for protected in ("torch", "torchvision", "torchaudio")
    )
    assert len(commands) == 1
    install = commands[0]
    assert install[install.index("--default-index") + 1] == (
        application.python_index_url
    )
    assert install[install.index("--constraint") + 1] == os.fspath(constraints)
    assert "download.pytorch.org" not in " ".join(install)
    assert install[install.index("--requirements") + 1] == os.fspath(temporary_paths[0])
    assert not temporary_paths[0].exists()


def test_ordinary_requirements_input_is_removed_after_child_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
    constraints = tmp_path / "python-package-constraints.txt"
    constraints.write_text("torch==2.12.1+cu130\n")
    temporary_paths: list[Path] = []

    def fail_run_argv(argv, **_kwargs) -> None:
        command = tuple(os.fspath(item) for item in argv)
        path = Path(command[command.index("--requirements") + 1])
        temporary_paths.append(path)
        assert path.read_text() == "numpy>=1.25\nrequests\n"
        raise ContainerCommandError("injected uv failure")

    monkeypatch.setattr(comfyui_installer, "_BUILD_DIRECTORY", tmp_path)
    monkeypatch.setattr(comfyui_installer, "run_argv", fail_run_argv)

    with pytest.raises(ContainerCommandError, match="injected uv failure"):
        comfyui_installer._install_ordinary_requirements(
            application,
            ("numpy>=1.25", "requests"),
            runtime,
            Path("/usr/local/bin/uv"),
            constraints,
            {},
        )

    assert len(temporary_paths) == 1
    assert not temporary_paths[0].exists()


def test_manager_requirements_input_is_removed_after_child_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
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
    requirements_paths: list[Path] = []

    def fail_run_argv(argv, **_kwargs) -> None:
        command = tuple(os.fspath(item) for item in argv)
        path = Path(command[command.index("--requirements") + 1])
        requirements_paths.append(path)
        assert path.read_text() == "comfyui_manager==4.0.5\n"
        raise ContainerCommandError("injected uv failure")

    monkeypatch.setattr(comfyui_installer, "_BUILD_DIRECTORY", tmp_path)
    monkeypatch.setattr(comfyui_installer, "run_argv", fail_run_argv)

    with pytest.raises(ContainerCommandError, match="injected uv failure"):
        comfyui_installer._install_manager_capability(
            application,
            manager,
            parsed,
            runtime,
            Path("/usr/local/bin/uv"),
            constraints,
            {},
        )

    assert len(requirements_paths) == 1
    assert not requirements_paths[0].exists()


# Manager capability binds package structure, distributions, and cm-cli ownership.
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "unavailable"),
        ("symlink", "real non-symlink directory"),
    ],
)
def test_manager_import_root_must_be_one_real_application_site_directory(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    application, runtime, anchor = _local_manager_application(tmp_path)
    manager = application.comfyui.manager
    assert manager is not None
    root = anchor.parent / manager.import_name
    if mutation == "symlink":
        target = tmp_path / "manager-root"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)

    with pytest.raises(ComfyUIInstallError, match=message):
        comfyui_installer._verify_manager_import_root(application, manager, runtime)


def test_manager_import_anchor_is_exclusive_read_only_and_exact(
    tmp_path: Path,
) -> None:
    application, runtime, anchor = _local_manager_application(tmp_path)
    manager = application.comfyui.manager
    assert manager is not None

    comfyui_installer._write_import_anchor(
        anchor,
        runtime.comfyui_path,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    comfyui_installer._verify_manager_import_anchor(
        application,
        manager,
        runtime,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert anchor.read_text() == f"{runtime.comfyui_path}\n"
    assert anchor.stat().st_mode & 0o777 == 0o444


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_manager_import_anchor_never_replaces_an_occupied_target(
    tmp_path: Path,
    kind: str,
) -> None:
    _application_plan, runtime, anchor = _local_manager_application(tmp_path)
    if kind == "file":
        anchor.write_text("foreign")
    elif kind == "directory":
        anchor.mkdir()
        (anchor / "owned").write_text("foreign")
    else:
        foreign = tmp_path / "foreign-anchor"
        foreign.write_text("foreign")
        anchor.symlink_to(foreign)

    with pytest.raises(ComfyUIInstallError, match="already exists"):
        comfyui_installer._write_import_anchor(
            anchor,
            runtime.comfyui_path,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    if kind == "file":
        assert anchor.read_text() == "foreign"
    elif kind == "directory":
        assert (anchor / "owned").read_text() == "foreign"
    else:
        assert anchor.is_symlink()
        assert anchor.readlink() == foreign


def test_manager_import_anchor_rejects_a_direct_symlink_parent(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-site-packages"
    real_parent.mkdir()
    linked_parent = tmp_path / "site-packages"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    anchor = linked_parent / "comfyui-docker-helper-comfyui.pth"

    with pytest.raises(ComfyUIInstallError, match="could not be written"):
        comfyui_installer._write_import_anchor(
            anchor,
            tmp_path / "ComfyUI",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert not (real_parent / anchor.name).exists()


def test_manager_import_anchor_post_creation_failure_leaves_the_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _application_plan, runtime, anchor = _local_manager_application(tmp_path)

    def fail_fchown(*_args) -> None:
        raise OSError(errno.EIO, "injected ownership failure")

    monkeypatch.setattr(comfyui_installer.os, "fchown", fail_fchown)

    with pytest.raises(ComfyUIInstallError, match="could not be written"):
        comfyui_installer._write_import_anchor(
            anchor,
            runtime.comfyui_path,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert anchor.is_file()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "unavailable"),
        ("content", "content does not match"),
        ("mode", "mode must be 0444"),
        ("owner", "ownership is invalid"),
        ("symlink", "regular non-symlink"),
        ("parent", "outside application site-packages"),
    ],
)
def test_manager_import_anchor_verifier_rejects_factual_drift(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    application, runtime, anchor = _local_manager_application(tmp_path)
    manager = application.comfyui.manager
    assert manager is not None
    observed_manager = manager
    owner_uid = os.getuid()
    if mutation == "symlink":
        target = tmp_path / "anchor-target"
        target.write_text(f"{runtime.comfyui_path}\n")
        anchor.symlink_to(target)
    elif mutation == "parent":
        observed_manager = manager.model_copy(
            update={
                "import_anchor": str(tmp_path / "other/site-packages" / anchor.name)
            }
        )
    elif mutation != "missing":
        anchor.write_text(
            "wrong\n" if mutation == "content" else f"{runtime.comfyui_path}\n"
        )
        anchor.chmod(0o644 if mutation == "mode" else 0o444)
        if mutation == "owner":
            owner_uid += 1

    with pytest.raises(ComfyUIInstallError, match=message):
        comfyui_installer._verify_manager_import_anchor(
            application,
            observed_manager,
            runtime,
            owner_uid=owner_uid,
            owner_gid=os.getgid(),
        )


def test_declared_manager_distributions_are_verified_from_application_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
    parsed = parse_manager_requirements(
        b"comfyui_manager==4.1b8\npackaging>=26\n",
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
    )
    observed_paths: list[list[str]] = []

    def distributions(*, path):
        observed_paths.append(path)
        return (
            SimpleNamespace(
                metadata={"Name": "ComfyUI_Manager"},
                version="4.1b8",
                entry_points=(
                    SimpleNamespace(
                        group="console_scripts", name="cm-cli", value="cm_cli:main"
                    ),
                ),
            ),
            SimpleNamespace(
                metadata={"Name": "packaging"},
                version="26.2",
                entry_points=(),
            ),
        )

    monkeypatch.setattr(
        comfyui_installer.importlib_metadata, "distributions", distributions
    )

    comfyui_installer._verify_declared_manager_distributions(
        application, parsed, runtime
    )

    assert observed_paths == [["/opt/venv/lib/python3.13/site-packages"]]


def test_declared_manager_distribution_mismatch_fails_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
    parsed = parse_manager_requirements(
        _MANAGER_REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
    )
    monkeypatch.setattr(
        comfyui_installer.importlib_metadata,
        "distributions",
        lambda **_kwargs: (
            SimpleNamespace(
                metadata={"Name": "comfyui-manager"},
                version="4.0.4",
                entry_points=(),
            ),
        ),
    )

    with pytest.raises(ComfyUIInstallError, match="does not satisfy"):
        comfyui_installer._verify_declared_manager_distributions(
            application, parsed, runtime
        )


@pytest.mark.parametrize("case", ["missing", "duplicate", "other-owner"])
def test_manager_cm_cli_requires_one_unique_distribution_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    application, runtime = _application(tmp_path)
    parsed = parse_manager_requirements(
        _MANAGER_REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
    )
    manager_entries = ()
    if case != "missing":
        manager_entries = (
            SimpleNamespace(
                group="console_scripts",
                name="cm-cli",
                value="comfyui_manager.cm_cli.__main__:main",
            ),
        )
    if case == "duplicate":
        manager_entries = (*manager_entries, manager_entries[0])
    distributions = [
        SimpleNamespace(
            metadata={"Name": "comfyui-manager"},
            version="4.0.5",
            entry_points=manager_entries,
        )
    ]
    if case == "other-owner":
        distributions.append(
            SimpleNamespace(
                metadata={"Name": "other"},
                version="1.0.0",
                entry_points=(
                    SimpleNamespace(
                        group="console_scripts", name="cm-cli", value="other:main"
                    ),
                ),
            )
        )
    monkeypatch.setattr(
        comfyui_installer.importlib_metadata,
        "distributions",
        lambda **_kwargs: tuple(distributions),
    )

    with pytest.raises(ComfyUIInstallError, match="console ownership"):
        comfyui_installer._verify_declared_manager_distributions(
            application, parsed, runtime
        )


@pytest.mark.parametrize("owner_name", [None, "invalid/name"])
def test_manager_cm_cli_rejects_unidentifiable_distribution_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_name: str | None,
) -> None:
    application, runtime = _application(tmp_path)
    parsed = parse_manager_requirements(
        _MANAGER_REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
    )
    distributions = (
        SimpleNamespace(
            metadata={"Name": "comfyui-manager"},
            version="4.0.5",
            entry_points=(
                SimpleNamespace(
                    group="console_scripts",
                    name="cm-cli",
                    value="comfyui_manager.cm_cli.__main__:main",
                ),
            ),
        ),
        SimpleNamespace(
            metadata={"Name": owner_name},
            version="1.0.0",
            entry_points=(
                SimpleNamespace(
                    group="console_scripts",
                    name="cm-cli",
                    value="unidentified:main",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        comfyui_installer.importlib_metadata,
        "distributions",
        lambda **_kwargs: distributions,
    )

    with pytest.raises(ComfyUIInstallError, match="unidentifiable"):
        comfyui_installer._verify_declared_manager_distributions(
            application, parsed, runtime
        )


# Custom-node installation reuses one immutable Manager authority per clean epoch.
def test_manager_capability_captures_and_reuses_immutable_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    manager = application.comfyui.manager
    assert manager is not None
    parsed = parse_manager_requirements(
        _MANAGER_REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
    )
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        comfyui_installer,
        "_read_manager_requirements",
        lambda observed_application, observed_manager, observed_path: (
            events.append(
                ("requirements", observed_application, observed_manager, observed_path)
            )
            or parsed
        ),
    )

    def record_complete_capability(
        observed_application,
        observed_manager,
        observed_parsed,
        observed_runtime,
    ) -> None:
        events.append(
            (
                "complete capability",
                observed_application,
                observed_manager,
                observed_parsed,
                observed_runtime,
            )
        )

    monkeypatch.setattr(
        comfyui_installer,
        "_verify_manager_capability",
        record_complete_capability,
    )

    authority = comfyui_installer.capture_manager_authority(
        application,
        runtime,
    )
    comfyui_installer.verify_manager_authority(application, runtime, authority)
    comfyui_installer.observe_manager_capability(application, runtime, authority)

    assert events == [
        ("requirements", application, manager, runtime.comfyui_path),
        ("complete capability", application, manager, parsed, runtime),
        ("requirements", application, manager, runtime.comfyui_path),
        ("complete capability", application, manager, parsed, runtime),
    ]


def test_manager_authority_rejects_same_semantics_content_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    runtime.comfyui_path.mkdir(parents=True)
    requirements = runtime.comfyui_path / "manager_requirements.txt"
    requirements.write_bytes(_MANAGER_REQUIREMENTS)
    monkeypatch.setattr(
        comfyui_installer, "_verify_manager_capability", lambda *_: None
    )

    authority = comfyui_installer.capture_manager_authority(
        application,
        runtime,
    )
    requirements.write_bytes(
        b"# same parsed requirements, different bytes\n" + _MANAGER_REQUIREMENTS
    )

    with pytest.raises(ComfyUIInstallError, match="authority changed"):
        comfyui_installer.verify_manager_authority(application, runtime, authority)


@pytest.mark.parametrize("kind", ["wrong-shebang", "not-executable", "symlink"])
def test_cm_cli_must_be_absolute_application_executable(
    tmp_path: Path, kind: str
) -> None:
    _application_plan, runtime = _application(tmp_path)
    executable = tmp_path / "cm-cli"
    target = tmp_path / "target"
    target.write_text(f"#!{runtime.python}\n")
    target.chmod(0o755)
    if kind == "symlink":
        executable.symlink_to(target)
    else:
        shebang = "/wrong/python" if kind == "wrong-shebang" else runtime.python
        executable.write_text(f"#!{shebang}\n")
        executable.chmod(0o644 if kind == "not-executable" else 0o755)

    with pytest.raises(ComfyUIInstallError, match=r"cm-cli|interpreter"):
        comfyui_installer._verify_cm_cli(
            executable,
            runtime,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_cm_cli_accepts_exact_application_shebang_and_owner(tmp_path: Path) -> None:
    _application_plan, runtime = _application(tmp_path)
    executable = tmp_path / "cm-cli"
    executable.write_text(f"#!{runtime.python}\n")
    executable.chmod(0o755)

    comfyui_installer._verify_cm_cli(
        executable,
        runtime,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
