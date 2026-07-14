"""Exact inference group installation and managed-constraint contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.unit.test_build_plan import accepted_resolution, final_config

from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    ExactPackagePlan,
    PackageGroupPlan,
    PyTorchGroupPlan,
    build_plan_digest,
    construct_build_plan,
    managed_constraints_bytes,
)
from comfyui_docker_helper.container import application_installer
from comfyui_docker_helper.container.application_installer import (
    ApplicationInstallError,
    _isolated_install_environment,
    _verify_application_imports,
    _verify_application_pip_commands,
    _verify_ordinary_requirements,
    _verify_resolution_manifest,
    _write_application_inventory,
    _write_constraints,
    install_inference_group,
    install_python_extras,
    verify_application_environment,
)
from comfyui_docker_helper.container.phase_inputs import phase_document
from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
)
from comfyui_docker_helper.pytorch_resolution import (
    pytorch_resolution_manifest_bytes,
)


def _write_phases(tmp_path: Path):
    plan = construct_build_plan(final_config(), accepted_resolution())
    digest = build_plan_digest(plan)
    application = tmp_path / "application.json"
    toolchain = tmp_path / "toolchain.json"
    application.write_text(
        phase_document("application", plan.application, digest).model_dump_json()
    )
    toolchain.write_text(
        phase_document("toolchain", plan.toolchain, digest).model_dump_json()
    )
    return plan, digest, application, toolchain


def _application_for_test_interpreter() -> ApplicationPhase:
    application = construct_build_plan(
        final_config(), accepted_resolution()
    ).application
    python_version = ".".join(str(item) for item in sys.version_info[:3])
    python_minor = ".".join(str(item) for item in sys.version_info[:2])
    document = application.model_dump(mode="python")
    document["pytorch"]["python_version"] = python_version
    document["comfyui"]["requirements"]["python_version"] = python_version
    if document["python_extras"] is not None:
        document["python_extras"]["python_version"] = python_version
    manager = document["comfyui"]["manager"]
    if manager is not None:
        manager["import_anchor"] = (
            f"/opt/venv/lib/python{python_minor}/site-packages/"
            "comfyui-docker-helper-comfyui.pth"
        )
    return ApplicationPhase.model_validate(document)


def _write_import_fixture(
    tmp_path: Path,
) -> tuple[ApplicationPhase, ContainerRuntime, Path, Path]:
    application = _application_for_test_interpreter()
    workspace = tmp_path / "ComfyUI"
    workspace.mkdir()
    workspace.joinpath("folder_paths.py").write_text("")
    comfy = workspace / "comfy"
    comfy.mkdir()
    comfy.joinpath("__init__.py").write_text("")
    virtual_env = tmp_path / "venv"
    virtual_env.joinpath("bin").mkdir(parents=True)
    virtual_env.joinpath("bin/python").symlink_to(sys.executable)
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    site_packages = virtual_env / "lib" / f"python{python_minor}" / "site-packages"
    site_packages.mkdir(parents=True)
    for module in ("torch", "torchaudio", "torchvision"):
        package = site_packages / module
        package.mkdir()
        package.joinpath("__init__.py").write_text("")
    runtime = ContainerRuntime(
        workspace=tmp_path,
        comfyui_path=workspace,
        virtual_env=virtual_env,
    )
    return application, runtime, workspace, site_packages


def _run_comfyui_capability_check(
    workspace: Path, *, extra_import_roots: tuple[Path, ...] = ()
) -> subprocess.CompletedProcess[str]:
    program = (
        "import sys\n"
        f"sys.path.extend({[os.fspath(path) for path in extra_import_roots]!r})\n"
        f"exec({application_installer._COMFYUI_CAPABILITY_CHECK!r})\n"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", program, os.fspath(workspace)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_install_uses_one_exact_group_and_explicit_application_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, digest, application, toolchain = _write_phases(tmp_path)
    constraints = tmp_path / "constraints.txt"
    manifest = tmp_path / "pyproject.toml"
    group = plan.application.pytorch
    manifest.write_bytes(
        pytorch_resolution_manifest_bytes(
            requirements=tuple(package.requirement for package in group.packages),
            direct_packages=tuple(package.name for package in group.packages),
            python_version=group.python_version,
            python_index_url=group.python_index_url,
            pytorch_index_url=group.pytorch_index_url,
        )
    )
    manifest.chmod(0o444)
    calls = []

    monkeypatch.setattr(
        application_installer, "_verify_resolution_manifest", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        application_installer,
        "_verify_setuptools_compatibility",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        application_installer,
        "_write_constraints",
        lambda path, content, **_kwargs: path.write_bytes(content),
    )
    monkeypatch.setattr(
        application_installer,
        "run_argv",
        lambda argv, **kwargs: calls.append((tuple(map(str, argv)), kwargs)),
    )
    runtime = ContainerRuntime(virtual_env=Path("/opt/venv"))

    install_inference_group(
        application,
        toolchain,
        expected_build_plan_digest=digest,
        runtime=runtime,
        constraints_path=constraints,
        resolution_manifest_path=manifest,
        environ={
            "UV_INDEX_URL": "https://poison.example",
            "PIP_CONSTRAINT": "/tmp/poison",
            "PYTHONPATH": "/tmp/poison",
            "HTTPS_PROXY": "https://proxy.example",
        },
    )

    install_argv, install_kwargs = calls[0]
    assert install_argv == (
        "/usr/local/bin/uv",
        "--no-config",
        "--project",
        str(tmp_path),
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "--no-python-downloads",
        "--requirements",
        str(manifest),
    )
    assert install_kwargs["env"] == {
        "HTTPS_PROXY": "https://proxy.example",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    verify_argv, _ = calls[1]
    assert verify_argv[:3] == ("/opt/venv/bin/python", "-I", "-c")
    assert json.loads(verify_argv[4]) == {
        "torch": "2.12.1+cu130",
        "torchaudio": "2.11.0+cu130",
        "torchvision": "0.27.1+cu130",
    }
    assert calls[2][0][:4] == (
        "/usr/local/bin/uv",
        "--no-config",
        "pip",
        "check",
    )
    assert constraints.read_bytes() == managed_constraints_bytes(group)
    assert not manifest.exists()


def test_constraints_are_complete_deterministic_and_read_only(tmp_path: Path) -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    constraints = tmp_path / "constraints.txt"
    _write_constraints(
        constraints,
        managed_constraints_bytes(plan.application.pytorch),
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert constraints.read_bytes() == (
        b"setuptools<82\ntorch==2.12.1+cu130\n"
        b"torchaudio==2.11.0+cu130\ntorchvision==0.27.1+cu130\n"
    )
    assert constraints.stat().st_mode & 0o777 == 0o444


def test_python_extras_install_exact_results_from_python_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    calls: list[tuple[tuple[str, ...], dict]] = []
    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)
    monkeypatch.setattr(
        application_installer,
        "run_argv",
        lambda argv, **kwargs: calls.append((tuple(map(str, argv)), kwargs)),
    )

    install_python_extras(
        plan.application,
        ContainerRuntime(virtual_env=Path("/opt/venv")),
        constraints_path=tmp_path / "constraints.txt",
        environ={"PIP_INDEX_URL": "https://poison.example/simple"},
    )

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == (
        "/usr/local/bin/uv",
        "--no-config",
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "--no-python-downloads",
        "--default-index",
        "https://pypi.org/simple",
        "--constraint",
        str(tmp_path / "constraints.txt"),
        "--",
        "numpy==2.3.1",
    )
    assert "PIP_INDEX_URL" not in kwargs["env"]
    assert kwargs["env"]["PIP_CONSTRAINT"] == str(tmp_path / "constraints.txt")
    assert kwargs["env"]["UV_CONSTRAINT"] == str(tmp_path / "constraints.txt")


@pytest.mark.parametrize(
    "name", ["torch", "torchvision", "torchaudio", "pip", "setuptools"]
)
def test_runtime_rejects_forged_python_extra_owner_overlap_before_install(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = construct_build_plan(
        final_config(), accepted_resolution()
    ).application
    forged_group = PackageGroupPlan.model_construct(
        group="application-extra",
        python_version=application.pytorch.python_version,
        platform=application.pytorch.platform,
        index_url=application.python_index_url,
        packages=(
            ExactPackagePlan.model_construct(
                name=name,
                extras=(),
                version="2.12.1+cu130" if name == "torch" else "1.0.0",
                environment="application",
            ),
        ),
    )
    forged = ApplicationPhase.model_construct(
        **{
            **application.__dict__,
            "python_extras": forged_group,
        }
    )
    monkeypatch.setattr(
        application_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("invalid owner plan must not mutate"),
    )

    with pytest.raises(ApplicationInstallError, match="package owners overlap"):
        install_python_extras(forged, ContainerRuntime())


def test_runtime_rejects_python_extra_overlap_with_arbitrary_pytorch_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = construct_build_plan(
        final_config(), accepted_resolution()
    ).application
    xformers = ExactPackagePlan.model_construct(
        name="xformers",
        extras=(),
        version="0.0.35",
        environment="application",
    )
    forged_pytorch = PyTorchGroupPlan.model_construct(
        **{
            **application.pytorch.__dict__,
            "packages": (*application.pytorch.packages, xformers),
        }
    )
    forged_extras = PackageGroupPlan.model_construct(
        group="application-extra",
        python_version=application.pytorch.python_version,
        platform=application.pytorch.platform,
        index_url=application.python_index_url,
        packages=(xformers,),
    )
    forged = ApplicationPhase.model_construct(
        **{
            **application.__dict__,
            "python_extras": forged_extras,
            "pytorch": forged_pytorch,
        }
    )
    monkeypatch.setattr(
        application_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("invalid owner plan must not mutate"),
    )

    with pytest.raises(ApplicationInstallError, match="package owners overlap"):
        install_python_extras(forged, ContainerRuntime())


def test_final_application_verification_checks_direct_identities_and_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    document = plan.application.model_dump(mode="python")
    document["inventory_path"] = "/opt/cdh/build/application-inventory.txt"
    application = type(plan.application).model_validate(document)
    inventory = (
        ("numpy", "2.3.1"),
        ("pip", "26.1.2"),
        ("setuptools", "81.0.0"),
        ("torch", "2.12.1+cu130"),
        ("torchaudio", "2.11.0+cu130"),
        ("torchvision", "0.27.1+cu130"),
    )
    calls: list[tuple[str, ...]] = []
    written: list[tuple[Path, bytes]] = []
    monkeypatch.setattr(application_installer, "_verify_constraints", lambda *_: None)
    monkeypatch.setattr(
        application_installer, "_application_inventory", lambda *_: inventory
    )
    monkeypatch.setattr(
        application_installer,
        "_verify_application_pip_commands",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        application_installer,
        "_verify_application_imports",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        application_installer,
        "run_argv",
        lambda argv, **_kwargs: calls.append(tuple(map(str, argv))),
    )
    monkeypatch.setattr(
        application_installer,
        "_write_application_inventory",
        lambda path, content: written.append((path, content)),
    )

    verify_application_environment(
        application,
        ContainerRuntime(virtual_env=Path("/opt/venv")),
        constraints_path=tmp_path / "constraints.txt",
        write_inventory=True,
    )

    assert calls == [
        (
            "/usr/local/bin/uv",
            "--no-config",
            "pip",
            "check",
            "--python",
            "/opt/venv/bin/python",
            "--no-python-downloads",
        )
    ]
    assert written == [
        (
            Path("/opt/cdh/build/application-inventory.txt"),
            b"numpy==2.3.1\npip==26.1.2\nsetuptools==81.0.0\n"
            b"torch==2.12.1+cu130\ntorchaudio==2.11.0+cu130\n"
            b"torchvision==0.27.1+cu130\n",
        )
    ]


def test_final_application_verification_reports_missing_setuptools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = construct_build_plan(
        final_config(), accepted_resolution()
    ).application
    inventory = (
        ("numpy", "2.3.1"),
        ("pip", "26.1.2"),
        ("torch", "2.12.1+cu130"),
        ("torchaudio", "2.11.0+cu130"),
        ("torchvision", "0.27.1+cu130"),
    )
    monkeypatch.setattr(application_installer, "_verify_constraints", lambda *_: None)
    monkeypatch.setattr(
        application_installer, "_application_inventory", lambda *_: inventory
    )

    with pytest.raises(
        ApplicationInstallError,
        match="installed setuptools does not satisfy PyTorch wheel metadata",
    ):
        verify_application_environment(
            application,
            ContainerRuntime(virtual_env=Path("/opt/venv")),
            constraints_path=tmp_path / "constraints.txt",
        )


def test_final_application_inventory_rejects_observation_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = construct_build_plan(
        final_config(), accepted_resolution()
    ).application
    inventory = (
        ("numpy", "2.3.1"),
        ("pip", "26.1.2"),
        ("setuptools", "81.0.0"),
        ("torch", "2.12.1+cu130"),
        ("torchaudio", "2.11.0+cu130"),
        ("torchvision", "0.27.1+cu130"),
    )
    observations = iter((inventory, (*inventory, ("unexpected", "1.0"))))
    monkeypatch.setattr(application_installer, "_verify_constraints", lambda *_: None)
    monkeypatch.setattr(
        application_installer, "_application_inventory", lambda *_: next(observations)
    )
    monkeypatch.setattr(
        application_installer,
        "_verify_application_pip_commands",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        application_installer,
        "_verify_application_imports",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        application_installer, "run_argv", lambda *_args, **_kwargs: None
    )

    with pytest.raises(
        ApplicationInstallError,
        match="application inventory changed during final verification",
    ):
        verify_application_environment(
            application,
            ContainerRuntime(virtual_env=Path("/opt/venv")),
            constraints_path=tmp_path / "constraints.txt",
            write_inventory=True,
        )


def test_final_application_verification_rejects_unsatisfied_ordinary_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = construct_build_plan(
        final_config(), accepted_resolution()
    ).application
    inventory = (
        ("numpy", "2.3.1"),
        ("pip", "26.1.2"),
        ("requests", "2.0.0"),
        ("setuptools", "81.0.0"),
        ("torch", "2.12.1+cu130"),
        ("torchaudio", "2.11.0+cu130"),
        ("torchvision", "0.27.1+cu130"),
    )
    monkeypatch.setattr(application_installer, "_verify_constraints", lambda *_: None)
    monkeypatch.setattr(
        application_installer, "_application_inventory", lambda *_: inventory
    )

    with pytest.raises(
        ApplicationInstallError,
        match="installed requests does not satisfy ComfyUI requirements",
    ):
        verify_application_environment(
            application,
            ContainerRuntime(virtual_env=Path("/opt/venv")),
            constraints_path=tmp_path / "constraints.txt",
            ordinary_requirements=("requests>=3",),
        )


def test_ordinary_requirement_verification_uses_the_exact_target_markers() -> None:
    application = construct_build_plan(
        final_config(), accepted_resolution()
    ).application

    _verify_ordinary_requirements(
        application,
        (
            "missing>=1; python_version < '3.13'",
            "requests>=2; python_version == '3.13'",
        ),
        {"requests": "2.34.2"},
    )

    with pytest.raises(
        ApplicationInstallError,
        match="installed requests does not satisfy ComfyUI requirements",
    ):
        _verify_ordinary_requirements(
            application,
            ("requests>=3; python_full_version == '3.13.14'",),
            {"requests": "2.34.2"},
        )


def _write_pip_fixture(
    tmp_path: Path,
) -> tuple[ApplicationPhase, ContainerRuntime, Path, Path]:
    application = _application_for_test_interpreter()
    virtual_env = tmp_path / "venv"
    bin_dir = virtual_env / "bin"
    bin_dir.mkdir(parents=True)
    bin_dir.joinpath("python").symlink_to(sys.executable)
    virtual_env.joinpath("pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).resolve().parent}\n"
        "include-system-site-packages = false\n"
    )
    workspace = tmp_path / "ComfyUI"
    workspace.mkdir()
    runtime = ContainerRuntime(
        workspace=tmp_path,
        comfyui_path=workspace,
        virtual_env=virtual_env,
    )
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    site_packages = virtual_env / "lib" / f"python{python_minor}" / "site-packages"
    package = site_packages / "pip"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("")
    output = f"pip {application.pip_version} from {package} (python {python_minor})"
    package.joinpath("__main__.py").write_text(f"print({output!r})\n")
    metadata = site_packages / f"pip-{application.pip_version}.dist-info"
    metadata.mkdir()
    metadata.joinpath("METADATA").write_text(
        f"Metadata-Version: 2.4\nName: pip\nVersion: {application.pip_version}\n"
    )
    record_rows: list[str] = []
    for name in ("pip", "pip3"):
        command = bin_dir / name
        command_content = f"#!{runtime.python}\nprint({output!r})\n".encode()
        command.write_bytes(command_content)
        command.chmod(0o755)
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(command_content).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        record_rows.append(
            f"../../../bin/{name},sha256={digest},{len(command_content)}"
        )
    metadata.joinpath("RECORD").write_text("\n".join(record_rows) + "\n")
    return application, runtime, package, workspace


def test_application_pip_commands_bind_exact_owner_module_and_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime, _package, _workspace = _write_pip_fixture(tmp_path)
    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)

    _verify_application_pip_commands(
        application,
        runtime,
        {},
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    expected_version = ".".join(str(item) for item in sys.version_info[:3])
    expected_minor = ".".join(str(item) for item in sys.version_info[:2])
    assert application.pytorch.python_version == expected_version
    assert _package.parent == (
        runtime.virtual_env / "lib" / f"python{expected_minor}" / "site-packages"
    )


def test_application_pip_commands_reject_correct_shebang_fake_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime, package, _workspace = _write_pip_fixture(tmp_path)
    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    runtime.virtual_env.joinpath("bin/pip").write_text(
        f"#!{runtime.python}\n"
        f"print('pip {application.pip_version} from {package} "
        f"(python {python_minor})')\n"
        "# forged after installation\n"
    )
    runtime.virtual_env.joinpath("bin/pip").chmod(0o755)

    with pytest.raises(ContainerCommandError):
        _verify_application_pip_commands(
            application,
            runtime,
            {},
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


@pytest.mark.parametrize(
    "kind", ["missing-command", "symlink-package", "workspace-shadow"]
)
def test_application_pip_commands_reject_wrong_owner_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    application, runtime, package, workspace = _write_pip_fixture(tmp_path)
    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)
    if kind == "missing-command":
        runtime.virtual_env.joinpath("bin/pip3").unlink()
    elif kind == "symlink-package":
        outside = tmp_path / "outside-pip"
        package.rename(outside)
        package.symlink_to(outside, target_is_directory=True)
    else:
        shadow = workspace / "pip"
        shadow.mkdir()
        shadow.joinpath("__init__.py").write_text("")

    match = "pip3 executable is unavailable" if kind == "missing-command" else None
    with pytest.raises((ApplicationInstallError, ContainerCommandError), match=match):
        _verify_application_pip_commands(
            application,
            runtime,
            {},
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_application_pip_commands_reject_base_environment_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime, _package, _workspace = _write_pip_fixture(tmp_path)
    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    base_root = tmp_path / "base/site-packages/pip"
    base_root.mkdir(parents=True)
    command = runtime.virtual_env / "bin/pip"
    command_content = (
        f"#!{runtime.python}\n"
        f"print('pip {application.pip_version} from {base_root} "
        f"(python {python_minor})')\n"
    ).encode()
    command.write_bytes(command_content)
    command.chmod(0o755)
    digest = (
        base64.urlsafe_b64encode(hashlib.sha256(command_content).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    metadata = next(
        runtime.virtual_env.glob("lib/python*/site-packages/pip-*.dist-info/RECORD")
    )
    rows = metadata.read_text().splitlines()
    rows[0] = f"../../../bin/pip,sha256={digest},{len(command_content)}"
    metadata.write_text("\n".join(rows) + "\n")

    with pytest.raises(ApplicationInstallError, match="application pip owner"):
        _verify_application_pip_commands(
            application,
            runtime,
            {},
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_application_pip_commands_report_missing_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime, _package, _workspace = _write_pip_fixture(tmp_path)
    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)
    runtime.virtual_env.joinpath("bin/pip3").unlink()
    with pytest.raises(ApplicationInstallError, match="pip3 executable is unavailable"):
        _verify_application_pip_commands(
            application,
            runtime,
            {},
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_application_import_verification_owner_binds_pytorch_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, runtime, workspace, site_packages = _write_import_fixture(tmp_path)
    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)

    _verify_application_imports(application, runtime, {})

    workspace.joinpath("torchaudio.py").write_text(
        "raise AssertionError('workspace shadow was imported')\n"
    )
    with pytest.raises(ContainerCommandError):
        _verify_application_imports(application, runtime, {})

    workspace.joinpath("torchaudio.py").unlink()
    outside = tmp_path / "outside-package"
    site_packages.joinpath("torchaudio").rename(outside)
    site_packages.joinpath("torchaudio").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContainerCommandError):
        _verify_application_imports(application, runtime, {})


@pytest.mark.parametrize(
    "initialized",
    [False, True],
    ids=["formal-v0.11.0-namespace", "initialized"],
)
def test_application_import_verification_accepts_supported_comfy_package_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialized: bool,
) -> None:
    application, runtime, workspace, _site_packages = _write_import_fixture(tmp_path)
    if not initialized:
        workspace.joinpath("comfy/__init__.py").unlink()
    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)

    _verify_application_imports(application, runtime, {})


def test_comfyui_capability_promotes_existing_exact_workspace_anchor_once(
    tmp_path: Path,
) -> None:
    _application, _runtime, workspace, _site_packages = _write_import_fixture(tmp_path)
    workspace.joinpath("comfy/__init__.py").unlink()
    first = tmp_path / "first-import-root"
    second = tmp_path / "second-import-root"
    first.mkdir()
    second.mkdir()
    workspace_entry = os.fspath(workspace)
    workspace.joinpath("folder_paths.py").write_text(
        "import sys\n"
        f"assert sys.path[0] == {workspace_entry!r}\n"
        f"assert sys.path.count({workspace_entry!r}) == 1\n"
        f"assert sys.path.index({os.fspath(first)!r}) "
        f"< sys.path.index({os.fspath(second)!r})\n"
    )

    completed = _run_comfyui_capability_check(
        workspace,
        extra_import_roots=(first, workspace, second, workspace),
    )

    assert completed.returncode == 0, completed.stderr


def test_comfyui_capability_accepts_namespace_without_workspace_anchor(
    tmp_path: Path,
) -> None:
    _application, _runtime, workspace, _site_packages = _write_import_fixture(tmp_path)
    workspace.joinpath("comfy/__init__.py").unlink()

    completed = _run_comfyui_capability_check(workspace)

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "__path__.append('/tmp/comfy-shadow')",
        "__file__ = '/tmp/comfy-shadow/__init__.py'",
    ],
    ids=["runtime-path", "runtime-file"],
)
def test_application_import_verification_rejects_runtime_identity_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    application, runtime, workspace, _site_packages = _write_import_fixture(tmp_path)
    workspace.joinpath("comfy/__init__.py").write_text(f"{mutation}\n")
    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)

    with pytest.raises(ContainerCommandError):
        _verify_application_imports(application, runtime, {})


@pytest.mark.parametrize(
    "mutation_template",
    [
        "__file__ = {init_alias!r}",
        "__spec__.origin = {init_alias!r}",
        "__path__[:] = [{root_alias!r}]",
        "__spec__.submodule_search_locations[:] = [{root_alias!r}]",
    ],
    ids=["module-file", "spec-origin", "module-path", "spec-location"],
)
def test_comfyui_capability_rejects_post_import_raw_symlink_alias(
    tmp_path: Path,
    mutation_template: str,
) -> None:
    _application, _runtime, workspace, _site_packages = _write_import_fixture(tmp_path)
    alias = tmp_path / "ComfyUI-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    workspace.joinpath("comfy/__init__.py").write_text(
        mutation_template.format(
            init_alias=os.fspath(alias / "comfy/__init__.py"),
            root_alias=os.fspath(alias / "comfy"),
        )
        + "\n"
    )

    completed = _run_comfyui_capability_check(workspace)

    assert completed.returncode != 0


@pytest.mark.parametrize("shadow_kind", ["namespace", "regular"])
def test_comfyui_capability_rejects_mixed_or_shadowed_import_identity(
    tmp_path: Path,
    shadow_kind: str,
) -> None:
    _application, _runtime, workspace, _site_packages = _write_import_fixture(tmp_path)
    workspace.joinpath("comfy/__init__.py").unlink()
    outside = tmp_path / "outside"
    shadow = outside / "comfy"
    shadow.mkdir(parents=True)
    side_effect = tmp_path / "shadow-imported"
    if shadow_kind == "regular":
        shadow.joinpath("__init__.py").write_text(
            f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('bad')\n"
        )

    completed = _run_comfyui_capability_check(workspace, extra_import_roots=(outside,))

    assert completed.returncode != 0
    assert not side_effect.exists()


def test_comfyui_capability_rejects_distinct_raw_alias_of_namespace_root(
    tmp_path: Path,
) -> None:
    _application, _runtime, workspace, _site_packages = _write_import_fixture(tmp_path)
    workspace.joinpath("comfy/__init__.py").unlink()
    alias = tmp_path / "ComfyUI-alias"
    alias.symlink_to(workspace, target_is_directory=True)

    completed = _run_comfyui_capability_check(
        workspace,
        extra_import_roots=(alias,),
    )

    assert completed.returncode != 0


@pytest.mark.parametrize(
    "escape", ["workspace", "workspace_parent", "folder_paths", "comfy", "comfy_init"]
)
def test_application_import_verification_rejects_checkout_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    escape: str,
) -> None:
    application, runtime, workspace, _site_packages = _write_import_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    if escape == "workspace":
        relocated = outside / "ComfyUI"
        workspace.rename(relocated)
        workspace.symlink_to(relocated, target_is_directory=True)
    elif escape == "workspace_parent":
        real_parent = outside / "real-parent"
        real_parent.mkdir()
        workspace.rename(real_parent / "ComfyUI")
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        runtime = ContainerRuntime(
            workspace=runtime.workspace,
            comfyui_path=linked_parent / "ComfyUI",
            virtual_env=runtime.virtual_env,
        )
    elif escape == "folder_paths":
        workspace.joinpath("folder_paths.py").unlink()
        outside.joinpath("folder_paths.py").write_text("")
        workspace.joinpath("folder_paths.py").symlink_to(outside / "folder_paths.py")
    elif escape == "comfy":
        workspace.joinpath("comfy/__init__.py").unlink()
        workspace.joinpath("comfy").rmdir()
        outside.joinpath("comfy").mkdir()
        outside.joinpath("comfy/__init__.py").write_text("")
        workspace.joinpath("comfy").symlink_to(outside / "comfy")
    else:
        workspace.joinpath("comfy/__init__.py").unlink()
        outside.joinpath("comfy-init.py").write_text("")
        workspace.joinpath("comfy/__init__.py").symlink_to(outside / "comfy-init.py")
    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)

    with pytest.raises(ContainerCommandError):
        _verify_application_imports(application, runtime, {})


def test_application_inventory_creation_is_exclusive_read_only_and_exact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "application-inventory.txt"
    content = b"pip==26.1.2\ntorch==2.12.1+cu130\n"

    _write_application_inventory(
        path,
        content,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o777 == 0o444
    with pytest.raises(ApplicationInstallError, match="already exists"):
        _write_application_inventory(
            path,
            content,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_application_inventory_never_unlinks_replacement_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "application-inventory.txt"
    content = b"pip==26.1.2\n"
    original_read_bytes = Path.read_bytes
    replaced = False

    def replace_before_verification(item: Path) -> bytes:
        nonlocal replaced
        if item == path and not replaced:
            replaced = True
            item.unlink()
            item.write_bytes(b"replacement")
        return original_read_bytes(item)

    monkeypatch.setattr(Path, "read_bytes", replace_before_verification)

    with pytest.raises(ApplicationInstallError, match="target identity changed"):
        _write_application_inventory(
            path,
            content,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert original_read_bytes(path) == b"replacement"
    assert not list(tmp_path.glob(".application-inventory.txt.*"))


def test_application_inventory_rejects_same_bytes_and_mode_replacement_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "application-inventory.txt"
    content = b"pip==26.1.2\n"
    original_read_bytes = Path.read_bytes
    replaced = False

    def replace_before_read(item: Path) -> bytes:
        nonlocal replaced
        if item == path and not replaced:
            replaced = True
            item.unlink()
            item.write_bytes(content)
            item.chmod(0o444)
        return original_read_bytes(item)

    monkeypatch.setattr(Path, "read_bytes", replace_before_read)

    with pytest.raises(ApplicationInstallError, match="target identity changed"):
        _write_application_inventory(
            path,
            content,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert original_read_bytes(path) == content
    assert path.stat().st_mode & 0o777 == 0o444
    assert not list(tmp_path.glob(".application-inventory.txt.*"))


def test_application_inventory_rejects_replaced_temporary_without_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "application-inventory.txt"
    original_has_identity = application_installer._inventory_path_has_identity
    replacement: Path | None = None

    def replace_before_check(item: Path, identity) -> bool:
        nonlocal replacement
        if item.name.startswith(".application-inventory.txt.") and replacement is None:
            item.unlink()
            item.write_bytes(b"replacement")
            replacement = item
        return original_has_identity(item, identity)

    monkeypatch.setattr(
        application_installer, "_inventory_path_has_identity", replace_before_check
    )

    with pytest.raises(ApplicationInstallError, match="temporary identity changed"):
        _write_application_inventory(
            path,
            b"pip==26.1.2\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert replacement is not None
    assert replacement.read_bytes() == b"replacement"
    assert not path.exists()


def test_application_inventory_never_unlinks_replacement_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "application-inventory.txt"

    def replace_link(_source, target, **_kwargs) -> None:
        Path(target).write_bytes(b"replacement")

    monkeypatch.setattr(os, "link", replace_link)

    with pytest.raises(ApplicationInstallError, match="linked identity changed"):
        _write_application_inventory(
            path,
            b"pip==26.1.2\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert path.read_bytes() == b"replacement"
    assert not list(tmp_path.glob(".application-inventory.txt.*"))


def test_resolution_manifest_rejects_changed_identity(tmp_path: Path) -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text("[project]\nname='changed'\n")
    manifest.chmod(0o444)

    with pytest.raises(ApplicationInstallError, match="does not match BuildPlan"):
        _verify_resolution_manifest(
            manifest,
            plan.application,
            expected_owner_uid=os.getuid(),
            expected_owner_gid=os.getgid(),
        )


def test_install_rejects_cross_channel_phase_before_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plan, digest, application, toolchain = _write_phases(tmp_path)
    document = json.loads(toolchain.read_text())
    document["payload"]["pytorch_channel"] = "cu129"
    toolchain.write_text(json.dumps(document))
    monkeypatch.setattr(
        application_installer,
        "run_argv",
        lambda *_a, **_k: pytest.fail("invalid phases must not execute"),
    )

    with pytest.raises(ApplicationInstallError, match="backend does not match"):
        install_inference_group(
            application,
            toolchain,
            expected_build_plan_digest=digest,
            runtime=ContainerRuntime(virtual_env=Path("/opt/venv")),
            constraints_path=tmp_path / "unused",
            resolution_manifest_path=tmp_path / "unused-manifest",
        )


def test_install_environment_does_not_inherit_package_or_python_configuration() -> None:
    assert _isolated_install_environment(
        {
            "UV_INDEX": "poison",
            "PIP_INDEX_URL": "poison",
            "PIP_CONSTRAINT": "poison",
            "PYTHONPATH": "poison",
            "VIRTUAL_ENV": "poison",
            "HTTPS_PROXY": "https://proxy.example",
        }
    ) == {
        "HTTPS_PROXY": "https://proxy.example",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
