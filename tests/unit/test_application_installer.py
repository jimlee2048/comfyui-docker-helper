"""Exact inference group installation and managed-constraint contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from tests.unit.test_build_plan import accepted_resolution, final_config

from comfyui_docker_helper.config.build_plan import (
    build_plan_digest,
    construct_build_plan,
    managed_constraints_bytes,
)
from comfyui_docker_helper.container import application_installer
from comfyui_docker_helper.container.application_installer import (
    ApplicationInstallError,
    _isolated_install_environment,
    _verify_resolution_manifest,
    _write_constraints,
    install_inference_group,
)
from comfyui_docker_helper.container.phase_inputs import phase_document
from comfyui_docker_helper.container.runners import ContainerRuntime
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
