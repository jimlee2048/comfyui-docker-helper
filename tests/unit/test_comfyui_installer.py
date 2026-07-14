"""Exact detached ComfyUI staging and requirements verification contracts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.test_build_plan import accepted_resolution, final_config

from comfyui_docker_helper.comfyui_requirements import (
    CUDA_PROTECTED_REQUIREMENTS,
    parse_comfyui_requirements,
    parse_manager_requirements,
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
    verify_application_state,
)
from comfyui_docker_helper.container.phase_inputs import phase_document
from comfyui_docker_helper.container.runners import ContainerRuntime

_REQUIREMENTS = b"torch\ntorchvision\ntorchaudio\nnumpy>=1.25\n"
_MANAGER_REQUIREMENTS = b"comfyui_manager==4.0.5\n"


@pytest.fixture(autouse=True)
def _fixture_checkout_has_supported_ancestry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(comfyui_installer, "_verify_floor_ancestry", lambda *_: None)


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
            (stage / "manager_requirements.txt").write_bytes(_MANAGER_REQUIREMENTS)
            audio = stage / "comfy_extras/nodes_audio.py"
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
        "detached",
        "origin",
        "ancestry",
        "requirements",
        "manager requirements",
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
    parsed_manager = parse_manager_requirements(
        _MANAGER_REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
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
        application_path,
        toolchain_path,
        expected_build_plan_digest=digest,
        runtime=runtime,
    )

    assert events == [
        "checkout",
        "verify",
        "inference",
        "extras",
        "health",
        "ordinary",
        "health",
        "manager",
        "health",
    ]


def test_orchestration_disabled_manager_skips_mutation_and_checks_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    document = plan.application.model_dump(mode="python")
    document["comfyui"]["manager"] = None
    application = ApplicationPhase.model_validate(document)
    digest = "sha256:" + "a" * 64
    application_path = tmp_path / "application.json"
    toolchain_path = tmp_path / "toolchain.json"
    application_path.write_text(
        phase_document("application", application, digest).model_dump_json()
    )
    toolchain_path.write_text(
        phase_document("toolchain", plan.toolchain, digest).model_dump_json()
    )
    parsed = parse_comfyui_requirements(
        _REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        protected_names=CUDA_PROTECTED_REQUIREMENTS,
    )
    events: list[str] = []
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
        application_path,
        toolchain_path,
        expected_build_plan_digest=digest,
        runtime=runtime,
    )

    assert events == [
        "checkout",
        "verify",
        "inference",
        "extras",
        "health",
        "ordinary",
        "health",
        "manager absent",
        "health",
    ]


def test_final_application_state_rechecks_source_capability_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime = _application(tmp_path)
    parsed = parse_comfyui_requirements(
        _REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
        protected_names=CUDA_PROTECTED_REQUIREMENTS,
    )
    parsed_manager = parse_manager_requirements(
        _MANAGER_REQUIREMENTS,
        python_version="3.13.14",
        platform="linux/amd64",
    )
    events: list[object] = []
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_stage_identity",
        lambda *_args: events.append("source"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_checkout",
        lambda *_args: events.append("checkout") or (parsed, parsed_manager),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_manager_capability",
        lambda *_args: events.append("manager"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "verify_application_environment",
        lambda *_args, **kwargs: events.append(
            (
                "application",
                kwargs["ordinary_requirements"],
                kwargs["write_inventory"],
            )
        ),
    )

    verify_application_state(application, runtime, write_inventory=True)

    assert events == [
        "source",
        "checkout",
        "manager",
        ("application", parsed.ordinary, True),
    ]


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
    assert len(commands) == 1
    install = commands[0]
    assert install[install.index("--default-index") + 1] == (
        application.python_index_url
    )
    assert install[install.index("--constraint") + 1] == os.fspath(constraints)
    assert "download.pytorch.org" not in " ".join(install)
    assert install[install.index("--requirements") + 1] == os.fspath(temporary_paths[0])
    assert not temporary_paths[0].exists()


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
            assert path.stat().st_mode & 0o777 == 0o444

    monkeypatch.setattr(comfyui_installer, "_BUILD_DIRECTORY", tmp_path)
    monkeypatch.setattr(comfyui_installer, "run_argv", fake_run_argv)
    monkeypatch.setattr(
        comfyui_installer,
        "_write_import_anchor",
        lambda path, workspace: events.append(f"anchor:{path}:{workspace}"),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_declared_manager_distributions",
        lambda *_args: events.append("declared distributions"),
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
        {},
    )

    assert len(commands) == 2
    install, install_kwargs = commands[0]
    assert install[install.index("--default-index") + 1] == (
        application.python_index_url
    )
    assert install[install.index("--constraint") + 1] == os.fspath(constraints)
    assert "download.pytorch.org" not in " ".join(install)
    assert install_kwargs["env"]["UV_CONSTRAINT"] == os.fspath(constraints)
    assert install_kwargs["env"]["PIP_CONSTRAINT"] == os.fspath(constraints)
    verify, verify_kwargs = commands[1]
    assert verify[0] == os.fspath(runtime.python)
    assert verify[-2] == os.fspath(Path(manager.import_anchor).parent)
    assert verify[-1] == manager.import_name
    assert verify_kwargs["env"]["COMFYUI_PATH"] == os.fspath(runtime.comfyui_path)
    assert verify_kwargs["env"]["VIRTUAL_ENV"] == os.fspath(runtime.virtual_env)
    assert events == [
        f"anchor:{manager.import_anchor}:{runtime.comfyui_path}",
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


def test_manager_import_anchor_is_exclusive_read_only_and_exact(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "venv/lib/python3.13/site-packages"
    site_packages.mkdir(parents=True)
    anchor = site_packages / "comfyui-docker-helper-comfyui.pth"
    workspace = tmp_path / "workspace/ComfyUI"

    comfyui_installer._write_import_anchor(
        anchor,
        workspace,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert anchor.read_text() == f"{workspace}\n"
    assert anchor.stat().st_mode & 0o777 == 0o444
    with pytest.raises(ComfyUIInstallError, match="already exists"):
        comfyui_installer._write_import_anchor(
            anchor,
            workspace,
            owner_uid=os.getuid(),
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


def test_registry_manager_capability_captures_and_reuses_immutable_authority(
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
        observed_environ,
    ) -> None:
        events.append(
            (
                "complete capability",
                observed_application,
                observed_manager,
                observed_parsed,
                observed_runtime,
                observed_environ,
            )
        )

    monkeypatch.setattr(
        comfyui_installer,
        "_verify_manager_capability",
        record_complete_capability,
    )

    authority = comfyui_installer.capture_manager_registry_authority(
        application,
        runtime,
    )
    comfyui_installer.verify_manager_registry_capability(
        application,
        runtime,
        authority,
    )

    assert events == [
        ("requirements", application, manager, runtime.comfyui_path),
        ("complete capability", application, manager, parsed, runtime, None),
        ("requirements", application, manager, runtime.comfyui_path),
        ("complete capability", application, manager, parsed, runtime, None),
    ]


def test_registry_manager_authority_rejects_same_semantics_content_drift(
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

    authority = comfyui_installer.capture_manager_registry_authority(
        application,
        runtime,
    )
    requirements.write_bytes(
        b"# same parsed requirements, different bytes\n" + _MANAGER_REQUIREMENTS
    )

    with pytest.raises(ComfyUIInstallError, match="authority changed"):
        comfyui_installer.verify_manager_registry_capability(
            application,
            runtime,
            authority,
        )


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


def test_manager_verification_programs_are_valid_python() -> None:
    compile(comfyui_installer._MANAGER_CAPABILITY_CHECK, "<manager-check>", "exec")
    compile(comfyui_installer._MANAGER_ABSENCE_CHECK, "<manager-absence>", "exec")


def test_manager_capability_accepts_realistic_namespace_package(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    (site_packages / "comfyui_manager").mkdir()

    namespace_probe = _probe_namespace_file(site_packages, "comfyui_manager")
    completed = _run_manager_capability_check(workspace, site_packages)

    assert namespace_probe.returncode == 0, namespace_probe.stderr
    assert completed.returncode == 0, completed.stderr


def test_manager_capability_accepts_safe_regular_package_after_validation(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    package = site_packages / "comfyui_manager"
    package.mkdir()
    side_effect = tmp_path / "safe-imported"
    package.joinpath("__init__.py").write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('ok')\n"
    )

    completed = _run_manager_capability_check(workspace, site_packages)

    assert completed.returncode == 0, completed.stderr
    assert side_effect.read_text() == "ok"


def test_manager_capability_rejects_outside_regular_without_importing_it(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    (site_packages / "comfyui_manager").mkdir()
    outside_root = tmp_path / "outside"
    outside = outside_root / "comfyui_manager"
    outside.mkdir(parents=True)
    side_effect = tmp_path / "outside-imported"
    outside.joinpath("__init__.py").write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('bad')\n"
    )

    completed = _run_manager_capability_check(
        workspace,
        site_packages,
        extra_import_roots=(outside_root,),
    )

    assert completed.returncode != 0
    assert "Manager import locations escape application site-packages" in (
        completed.stderr
    )
    assert not side_effect.exists()


def test_manager_capability_rejects_mixed_namespace_locations(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    (site_packages / "comfyui_manager").mkdir()
    outside_root = tmp_path / "outside"
    (outside_root / "comfyui_manager").mkdir(parents=True)

    completed = _run_manager_capability_check(
        workspace,
        site_packages,
        extra_import_roots=(outside_root,),
    )

    assert completed.returncode != 0
    assert "Manager import locations escape application site-packages" in (
        completed.stderr
    )


def test_manager_capability_rejects_regular_symlink_origin_without_importing_it(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    package = site_packages / "comfyui_manager"
    package.mkdir()
    side_effect = tmp_path / "symlink-origin-imported"
    outside_initializer = tmp_path / "outside/__init__.py"
    outside_initializer.parent.mkdir()
    outside_initializer.write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('bad')\n"
    )
    package.joinpath("__init__.py").symlink_to(outside_initializer)

    completed = _run_manager_capability_check(workspace, site_packages)

    assert completed.returncode != 0
    assert "Manager import origin escapes application root" in completed.stderr
    assert not side_effect.exists()


def test_manager_capability_rejects_symlinked_import_root_outside_application(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    outside = tmp_path / "outside/comfyui_manager"
    outside.mkdir(parents=True)
    (site_packages / "comfyui_manager").symlink_to(
        outside,
        target_is_directory=True,
    )

    completed = _run_manager_capability_check(workspace, site_packages)

    assert completed.returncode != 0
    assert "Manager import root escapes application site-packages" in completed.stderr


def test_manager_capability_rejects_symlink_alias_inside_application_site(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    alias = site_packages / "manager-alias"
    alias.mkdir()
    (site_packages / "comfyui_manager").symlink_to(alias, target_is_directory=True)

    completed = _run_manager_capability_check(workspace, site_packages)

    assert completed.returncode != 0
    assert "Manager import root escapes application site-packages" in completed.stderr


def test_manager_capability_rejects_workspace_first_shadow_without_importing_it(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    (site_packages / "comfyui_manager").mkdir()
    shadow = workspace / "comfyui_manager"
    shadow.mkdir()
    side_effect = tmp_path / "workspace-manager-imported"
    shadow.joinpath("__init__.py").write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('bad')\n"
    )

    completed = _run_manager_capability_check(workspace, site_packages)

    assert completed.returncode != 0
    assert "Manager import locations escape application site-packages" in (
        completed.stderr
    )
    assert not side_effect.exists()


def test_comfy_capability_accepts_realistic_checkout_namespace(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    (site_packages / "comfyui_manager").mkdir()

    namespace_probe = _probe_namespace_file(workspace, "comfy")
    completed = _run_manager_capability_check(workspace, site_packages)

    assert namespace_probe.returncode == 0, namespace_probe.stderr
    assert completed.returncode == 0, completed.stderr


def test_comfy_capability_accepts_safe_regular_checkout_package(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    (site_packages / "comfyui_manager").mkdir()
    side_effect = tmp_path / "safe-comfy-imported"
    workspace.joinpath("comfy/__init__.py").write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('ok')\n"
    )

    completed = _run_manager_capability_check(workspace, site_packages)

    assert completed.returncode == 0, completed.stderr
    assert side_effect.read_text() == "ok"


def test_comfy_capability_rejects_outside_regular_without_importing_it(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    (site_packages / "comfyui_manager").mkdir()
    outside_root = tmp_path / "outside"
    outside = outside_root / "comfy"
    outside.mkdir(parents=True)
    side_effect = tmp_path / "outside-comfy-imported"
    outside.joinpath("__init__.py").write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('bad')\n"
    )

    completed = _run_manager_capability_check(
        workspace,
        site_packages,
        extra_import_roots=(outside_root,),
    )

    assert completed.returncode != 0
    assert "ComfyUI comfy import locations escape checkout" in completed.stderr
    assert not side_effect.exists()


def test_comfy_capability_rejects_mixed_namespace_locations(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    (site_packages / "comfyui_manager").mkdir()
    outside_root = tmp_path / "outside"
    (outside_root / "comfy").mkdir(parents=True)

    completed = _run_manager_capability_check(
        workspace,
        site_packages,
        extra_import_roots=(outside_root,),
    )

    assert completed.returncode != 0
    assert "ComfyUI comfy import locations escape checkout" in completed.stderr


def test_comfy_capability_rejects_symlink_origin_without_importing_it(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    (site_packages / "comfyui_manager").mkdir()
    side_effect = tmp_path / "symlink-comfy-origin-imported"
    outside_initializer = tmp_path / "outside/__init__.py"
    outside_initializer.parent.mkdir()
    outside_initializer.write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('bad')\n"
    )
    workspace.joinpath("comfy/__init__.py").symlink_to(outside_initializer)

    completed = _run_manager_capability_check(workspace, site_packages)

    assert completed.returncode != 0
    assert "ComfyUI comfy import origin escapes checkout root" in completed.stderr
    assert not side_effect.exists()


def test_comfy_capability_rejects_symlinked_root_outside_checkout(
    tmp_path: Path,
) -> None:
    workspace, site_packages = _manager_capability_fixture(tmp_path)
    (site_packages / "comfyui_manager").mkdir()
    workspace.joinpath("comfy").rmdir()
    outside = tmp_path / "outside/comfy"
    outside.mkdir(parents=True)
    workspace.joinpath("comfy").symlink_to(outside, target_is_directory=True)

    completed = _run_manager_capability_check(workspace, site_packages)

    assert completed.returncode != 0
    assert "ComfyUI comfy import root escapes checkout" in completed.stderr


def test_disabled_manager_rejects_stray_import_tree_without_importing_it(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "site-packages"
    package = import_root / "comfyui_manager"
    package.mkdir(parents=True)
    side_effect = tmp_path / "imported"
    package.joinpath("__init__.py").write_text(
        f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('imported')\n"
    )
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(import_root)!r})\n"
        f"exec({comfyui_installer._MANAGER_ABSENCE_CHECK!r})\n"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode != 0
    assert "Manager import exists while disabled" in completed.stderr
    assert not side_effect.exists()


def _manager_capability_fixture(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "ComfyUI"
    workspace.mkdir()
    (workspace / "folder_paths.py").write_text("")
    comfy = workspace / "comfy"
    comfy.mkdir()
    site_packages = tmp_path / "venv/lib/python3.13/site-packages"
    site_packages.mkdir(parents=True)
    metadata = site_packages / "comfyui_manager-4.0.5.dist-info"
    metadata.mkdir()
    metadata.joinpath("METADATA").write_text(
        "Metadata-Version: 2.4\nName: comfyui-manager\nVersion: 4.0.5\n"
    )
    metadata.joinpath("entry_points.txt").write_text(
        "[console_scripts]\ncm-cli = comfyui_manager.cm_cli.__main__:main\n"
    )
    return workspace, site_packages


def _run_manager_capability_check(
    workspace: Path,
    site_packages: Path,
    *,
    extra_import_roots: tuple[Path, ...] = (),
) -> subprocess.CompletedProcess[str]:
    import_roots = (workspace, site_packages, *extra_import_roots)
    program = (
        "import sys\n"
        f"sys.path[:0] = {[os.fspath(path) for path in import_roots]!r}\n"
        f"exec({comfyui_installer._MANAGER_CAPABILITY_CHECK!r})\n"
    )
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            program,
            "4.0.5",
            os.fspath(workspace),
            os.fspath(site_packages),
            "comfyui_manager",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _probe_namespace_file(
    import_root: Path,
    package_name: str,
) -> subprocess.CompletedProcess[str]:
    program = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {os.fspath(import_root)!r})\n"
        f"assert importlib.import_module({package_name!r}).__file__ is None\n"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", program],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _git(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()
