"""Shared Git/local root installation contracts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from comfyui_docker_helper.container.build.custom_nodes import root_install
from comfyui_docker_helper.container.build.custom_nodes.contracts import (
    CustomNodeInstallError,
)
from tests.container_installer_support import application as _application


def test_root_installer_runs_only_root_requirements_then_install_py(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    target = runtime.comfyui_path / "custom_nodes/direct"
    target.mkdir()
    target.joinpath("requirements.txt").write_text(
        "nvidia-ml-py # for GPU util/power/temp only\n"
    )
    target.joinpath("install.py").write_text("print('root')\n")
    nested = target / "nested"
    nested.mkdir()
    nested.joinpath("requirements.txt").write_text("must-not-run==9\n")
    nested.joinpath("install.py").write_text("raise RuntimeError\n")
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    events: list[str] = []

    def run(argv, **kwargs):
        commands.append((tuple(os.fspath(item) for item in argv), kwargs))
        events.append("requirements" if "--requirements" in argv else "install.py")

    monkeypatch.setattr(root_install, "run_argv", run)
    constraints = tmp_path / "constraints.txt"
    python_environment = {
        "BUILD_ENV_SENTINEL": "kept",
        "PIP_CONSTRAINT": str(constraints),
        "UV_CONSTRAINT": str(constraints),
    }
    root_install.install_root_surfaces(
        "Custom node direct",
        target,
        application,
        runtime,
        Path("/usr/local/bin/uv"),
        constraints,
        python_environment,
    )

    assert len(commands) == 2
    assert events == ["requirements", "install.py"]
    requirements_argv, requirements_kwargs = commands[0]
    assert requirements_argv == (
        "/usr/local/bin/uv",
        "--no-config",
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "--no-python-downloads",
        "--default-index",
        application.python_index_url,
        "--constraint",
        str(constraints),
        "--requirements",
        str(target / "requirements.txt"),
    )
    assert requirements_kwargs["close_stdin"] is True
    install_argv, install_kwargs = commands[1]
    assert install_argv == ("/opt/venv/bin/python", str(target / "install.py"))
    assert install_kwargs["close_stdin"] is True
    for command_kwargs in (requirements_kwargs, install_kwargs):
        assert command_kwargs["env"] == python_environment


def test_root_installer_rejects_symlinked_surface(tmp_path: Path) -> None:
    application, runtime = _application(tmp_path)
    target = runtime.comfyui_path / "custom_nodes/direct"
    target.mkdir()
    outside = tmp_path / "requirements.txt"
    outside.write_text("example==1.0\n")
    target.joinpath("requirements.txt").symlink_to(outside)

    with pytest.raises(CustomNodeInstallError, match="one regular file"):
        root_install.install_root_surfaces(
            "Custom node direct",
            target,
            application,
            runtime,
            Path("/usr/local/bin/uv"),
            tmp_path / "constraints.txt",
            {},
        )


@pytest.mark.parametrize(
    "requirement",
    [
        "--index-url https://packages.invalid/simple\nexample==1\n",
        "--extra-index-url https://packages.invalid/simple\nexample==1\n",
        "-r nested.txt\n",
        "-c constraints.txt\n",
        "example @ https://packages.invalid/example.whl\n",
    ],
)
def test_root_requirements_reject_source_control_before_any_install_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requirement: str,
) -> None:
    application, runtime = _application(tmp_path)
    target = runtime.comfyui_path / "custom_nodes/direct"
    target.mkdir()
    target.joinpath("requirements.txt").write_text(requirement)
    target.joinpath("install.py").write_text("raise RuntimeError\n")
    monkeypatch.setattr(
        root_install,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("invalid requirements must not execute"),
    )

    with pytest.raises(CustomNodeInstallError, match="requirements are invalid"):
        root_install.install_root_surfaces(
            "Custom node direct",
            target,
            application,
            runtime,
            Path("/usr/local/bin/uv"),
            tmp_path / "constraints.txt",
            {},
        )
