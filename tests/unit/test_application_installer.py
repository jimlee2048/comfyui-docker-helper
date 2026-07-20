"""Exact inference group installation and managed-constraint contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from tests.unit.test_build_plan import accepted_resolution, build_plan, final_config

from comfyui_docker_helper.application_checkers import APPLICATION_CHECKER_SOURCE
from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    ExactPackagePlan,
    PackageGroupPlan,
    PyTorchGroupPlan,
    build_plan_digest,
    managed_constraints_bytes,
)
from comfyui_docker_helper.container import application_installer
from comfyui_docker_helper.container.application_installer import (
    ApplicationInstallError,
    _verify_application_imports,
    _verify_application_pip_commands,
    _verify_ordinary_requirements,
    _write_application_inventory,
    _write_constraints,
    application_install_environment,
    install_inference_group,
    install_python_extras,
    verify_application_environment,
)
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.pytorch_resolution import (
    pytorch_resolution_manifest_bytes,
)


def _write_phases(tmp_path: Path):
    plan = build_plan(final_config(), accepted_resolution())
    digest = build_plan_digest(plan)
    return plan, digest, plan.application, plan.toolchain


@pytest.fixture(autouse=True)
def _materialized_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        application_installer,
        "APPLICATION_CHECKER_CONTAINER_PATH",
        APPLICATION_CHECKER_SOURCE,
    )


# Application installation consumes one exact source-routed plan and emits exact
# evidence.
def test_install_uses_one_exact_group_and_explicit_application_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _digest, application, toolchain = _write_phases(tmp_path)
    constraints = tmp_path / "constraints.txt"
    group = plan.application.pytorch
    calls = []
    observed_manifest: list[bytes] = []

    def record_call(argv, **kwargs) -> None:
        calls.append((tuple(map(str, argv)), kwargs))
        if "--requirements" in argv:
            observed_manifest.append(Path(argv[-1]).read_bytes())

    monkeypatch.setattr(application_installer, "_BUILD_DIRECTORY", tmp_path)
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
        record_call,
    )
    runtime = ContainerRuntime(virtual_env=Path("/opt/venv"))

    install_inference_group(
        application,
        toolchain,
        runtime=runtime,
        constraints_path=constraints,
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
        str(Path(install_argv[3])),
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "--no-python-downloads",
        "--requirements",
        install_argv[-1],
    )
    resolution_root = Path(install_argv[3])
    assert resolution_root.name.startswith(".pytorch-resolution-")
    assert Path(install_argv[-1]).parent == resolution_root
    assert observed_manifest == [
        pytorch_resolution_manifest_bytes(
            requirements=tuple(package.requirement for package in group.packages),
            direct_packages=tuple(package.name for package in group.packages),
            python_version=group.python_version,
            python_index_url=group.python_index_url,
            pytorch_index_url=group.pytorch_index_url,
        )
    ]
    assert not resolution_root.exists()
    assert install_kwargs["env"] == {
        "HTTPS_PROXY": "https://proxy.example",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    verify_argv, _ = calls[1]
    assert verify_argv[:4] == (
        "/opt/venv/bin/python",
        "-I",
        str(APPLICATION_CHECKER_SOURCE),
        "inventory",
    )
    assert json.loads(verify_argv[4]) == {
        "distributions": {
            "torch": "2.12.1+cu130",
            "torchaudio": "2.11.0+cu130",
            "torchvision": "0.27.1+cu130",
        }
    }
    assert calls[2][0][:4] == (
        "/usr/local/bin/uv",
        "--no-config",
        "pip",
        "check",
    )
    assert constraints.read_bytes() == managed_constraints_bytes(group)


def test_constraints_are_complete_deterministic_and_read_only(tmp_path: Path) -> None:
    plan = build_plan(final_config(), accepted_resolution())
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
    plan = build_plan(final_config(), accepted_resolution())
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
    application = build_plan(final_config(), accepted_resolution()).application
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
    application = build_plan(final_config(), accepted_resolution()).application
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


# Final verification observes exact distributions, imports, requirements, and inventory.
def test_final_application_verification_checks_direct_identities_and_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
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
    application = build_plan(final_config(), accepted_resolution()).application
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
    application = build_plan(final_config(), accepted_resolution()).application
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
    application = build_plan(final_config(), accepted_resolution()).application
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
    application = build_plan(final_config(), accepted_resolution()).application

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


def test_pip_verification_constructs_checker_payload_and_command_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep installer coverage at the checker payload and command boundary."""
    application = build_plan(final_config(), accepted_resolution()).application
    virtual_env = tmp_path / "venv"
    bin_dir = virtual_env / "bin"
    bin_dir.mkdir(parents=True)
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    site_packages = virtual_env / "lib" / f"python{python_minor}" / "site-packages"
    site_packages.mkdir(parents=True)
    site_packages.joinpath("pip").mkdir()
    runtime = ContainerRuntime(
        comfyui_path=tmp_path / "ComfyUI",
        virtual_env=virtual_env,
    )
    runtime.comfyui_path.mkdir()
    for name in ("pip", "pip3"):
        command = bin_dir / name
        command.write_text(f"#!{runtime.python}\n")
        command.chmod(0o755)
    checker_calls: list[tuple[object, ...]] = []
    command_calls: list[tuple[tuple[Path | str, ...], dict[str, str], str]] = []
    monkeypatch.setattr(
        application_installer,
        "run_application_checker",
        lambda *args, **kwargs: checker_calls.append((*args, kwargs)),
    )

    def capture(argv, environment, description):
        command_calls.append((argv, environment, description))
        return (
            f"pip {application.pip_version} from {site_packages / 'pip'} "
            f"(python {python_minor})"
        )

    monkeypatch.setattr(application_installer, "_capture_application_command", capture)

    _verify_application_pip_commands(
        application,
        runtime,
        {"HTTPS_PROXY": "https://proxy.example"},
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    observed_runtime, capability, expected, kwargs = checker_calls[0]
    assert observed_runtime is runtime
    assert capability == "pip"
    assert expected == {
        "site_packages": os.fspath(site_packages),
        "workspace": os.fspath(runtime.comfyui_path),
        "version": application.pip_version,
        "commands": [os.fspath(bin_dir / "pip"), os.fspath(bin_dir / "pip3")],
    }
    assert kwargs["environ"] == {"HTTPS_PROXY": "https://proxy.example"}
    assert [call[0] for call in command_calls] == [
        (bin_dir / "pip", "--version"),
        (bin_dir / "pip3", "--version"),
        (runtime.python, "-I", "-m", "pip", "--version"),
    ]
    assert all(
        call[1]["HTTPS_PROXY"] == "https://proxy.example" for call in command_calls
    )


def test_import_verification_constructs_exact_checker_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project versions and runtime paths without repeating checker semantics."""
    application = build_plan(final_config(), accepted_resolution()).application
    xformers = ExactPackagePlan.model_construct(
        name="xformers",
        extras=(),
        version="0.0.35+cu130",
        environment="application",
    )
    pytorch = PyTorchGroupPlan.model_construct(
        **{
            **application.pytorch.__dict__,
            "packages": (*application.pytorch.packages, xformers),
        }
    )
    application = ApplicationPhase.model_construct(
        **{**application.__dict__, "pytorch": pytorch}
    )
    runtime = ContainerRuntime(
        comfyui_path=tmp_path / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        application_installer,
        "run_application_checker",
        lambda *args, **kwargs: calls.append((*args, kwargs)),
    )

    _verify_application_imports(application, runtime, {})

    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    site_packages = runtime.virtual_env / "lib" / f"python{python_minor}/site-packages"
    assert calls[0][1:3] == (
        "pytorch",
        {
            "site_packages": os.fspath(site_packages),
            "workspace": os.fspath(runtime.comfyui_path),
            "distributions": {
                "torch": "2.12.1+cu130",
                "torchaudio": "2.11.0+cu130",
                "torchvision": "0.27.1+cu130",
                "xformers": "0.0.35+cu130",
            },
            "modules": {
                "torch": "torch",
                "torchaudio": "torchaudio",
                "torchvision": "torchvision",
            },
        },
    )
    assert calls[1][1:3] == (
        "comfyui",
        {"workspace": os.fspath(runtime.comfyui_path)},
    )
    assert calls[1][3]["runtime_environment"] is True


# Evidence and manifest checks bind installed state before accepting the application.
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


def test_install_rejects_cross_channel_phase_before_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plan, _digest, application, toolchain = _write_phases(tmp_path)
    toolchain = toolchain.model_copy(update={"pytorch_channel": "cu129"})
    monkeypatch.setattr(
        application_installer,
        "run_argv",
        lambda *_a, **_k: pytest.fail("invalid phases must not execute"),
    )

    with pytest.raises(ApplicationInstallError, match="backend does not match"):
        install_inference_group(
            application,
            toolchain,
            runtime=ContainerRuntime(virtual_env=Path("/opt/venv")),
            constraints_path=tmp_path / "unused",
        )


def test_install_environment_does_not_inherit_package_or_python_configuration() -> None:
    assert application_install_environment(
        {
            "UV_INDEX": "poison",
            "PIP_INDEX_URL": "poison",
            "PIP_CONSTRAINT": "poison",
            "PYTHONPATH": "poison",
            "VIRTUAL_ENV": "poison",
            "HTTPS_PROXY": "https://proxy.example",
        },
        constraints_path=Path("/opt/cdh/build/constraints.txt"),
        comfyui_path=Path("/workspace/ComfyUI"),
        virtual_env=Path("/opt/venv"),
    ) == {
        "HTTPS_PROXY": "https://proxy.example",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PIP_CONSTRAINT": "/opt/cdh/build/constraints.txt",
        "UV_CONSTRAINT": "/opt/cdh/build/constraints.txt",
        "COMFYUI_PATH": "/workspace/ComfyUI",
        "VIRTUAL_ENV": "/opt/venv",
    }
