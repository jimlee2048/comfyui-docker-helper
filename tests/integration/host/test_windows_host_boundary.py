"""Native Windows checks for the public host/container import boundary."""

import subprocess
import sys

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows import and CLI behavior",
)

_NATIVE_WINDOWS_PROBE_TIMEOUT_SECONDS = 30
_LINUX_IMPLEMENTATION_MODULES = frozenset(
    {
        "comfyui_docker_helper.container.build_plan_input",
        "comfyui_docker_helper.container.final_manifest",
        "comfyui_docker_helper.container.runners",
        "comfyui_docker_helper.container.runtime_control_client",
        "comfyui_docker_helper.container.runtime_serve",
        "comfyui_docker_helper.host.secret_session",
    }
)


def test_root_cli_does_not_load_platform_implementations() -> None:
    """Keep root registration outside Linux and Secret implementation closures."""
    modules = repr(_LINUX_IMPLEMENTATION_MODULES)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import comfyui_docker_helper.cli; "
                f"forbidden = {modules}; "
                "loaded = sorted(forbidden.intersection(sys.modules)); "
                "assert not loaded, loaded"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=_NATIVE_WINDOWS_PROBE_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "command",
    [["runtime", "serve"], ["download-files"]],
    ids=["runtime", "build-helper"],
)
def test_container_execution_reports_linux_only_before_service_loading(
    command: list[str],
) -> None:
    """Reject representative runtime and required-option commands uniformly."""
    from comfyui_docker_helper.cli import app

    result = CliRunner().invoke(app, ["container", *command])

    assert result.exit_code == 1
    assert "run only inside" in result.output
    assert "Linux image" in result.output
    assert "cdh host" in result.output
    assert "traceback" not in result.output.lower()
