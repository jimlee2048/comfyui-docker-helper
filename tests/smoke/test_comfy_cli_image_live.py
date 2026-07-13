"""Opt-in disposable-image acceptance for the isolated comfy-cli user tool."""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.docker,
    pytest.mark.network,
    pytest.mark.slow,
]

_ENABLED_IMAGE = "CDH_COMFY_CLI_ENABLED_IMAGE"
_DISABLED_IMAGE = "CDH_COMFY_CLI_DISABLED_IMAGE"

_ENABLED_PROBE = r"""
set -eu

test -x /opt/uv/tools/comfy-cli/bin/python
test ! -e "$COMFYUI_PATH/.venv"
test ! -L "$COMFYUI_PATH/.venv"
test ! -e "$COMFYUI_PATH/venv"
test ! -L "$COMFYUI_PATH/venv"

/opt/uv/tools/comfy-cli/bin/python -c '
import importlib.metadata as metadata
import json
import pathlib
import sys

plan = json.loads(pathlib.Path("/opt/cdh/build/build-plan.json").read_text())
tool = plan["toolchain"]["tool_store"]["comfy_cli"]
assert tool is not None
assert metadata.version("comfy-cli") == tool["version"]
assert pathlib.Path(sys.prefix) == pathlib.Path("/opt/uv/tools/comfy-cli")
inventory = pathlib.Path(tool["inventory_path"]).read_text().splitlines()
assert "comfy-cli==" + tool["version"] in inventory
'
uv --no-config pip check \
  --python /opt/uv/tools/comfy-cli/bin/python \
  --no-python-downloads

for command in comfy comfy-cli comfycli; do
  public="/opt/uv/bin/$command"
  owned="/opt/uv/tools/comfy-cli/bin/$command"
  test -x "$public"
  test "$(readlink -f "$public")" = "$owned"
  "$public" --help >/dev/null
done

/opt/uv/bin/comfy --workspace="$COMFYUI_PATH" launch -- \
  --listen 127.0.0.1 --port 8199 --disable-auto-launch --cpu &
launcher="$!"
application_pid=""
cleanup() {
  trap - EXIT INT TERM
  for pid in "$application_pid" "$launcher"; do
    test -z "$pid" || kill "$pid" 2>/dev/null || true
  done

  cleanup_attempt=0
  while [ "$cleanup_attempt" -lt 10 ]; do
    alive=""
    for pid in "$application_pid" "$launcher"; do
      test -n "$pid" || continue
      if test -d "/proc/$pid" && \
        ! grep -q '^State:[[:space:]]*Z' "/proc/$pid/status"; then
        alive=1
      fi
    done
    test -n "$alive" || break
    cleanup_attempt=$((cleanup_attempt + 1))
    sleep 1
  done

  for pid in "$application_pid" "$launcher"; do
    test -z "$pid" || kill -KILL "$pid" 2>/dev/null || true
  done
  test -z "$application_pid" || wait "$application_pid" 2>/dev/null || true
  wait "$launcher" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

attempt=0
while [ "$attempt" -lt 180 ]; do
  for process in /proc/[0-9]*; do
    test -r "$process/cmdline" || continue
    first="$(tr '\000' '\n' < "$process/cmdline" | sed -n '1p')"
    test "$first" = "/opt/venv/bin/python" || continue
    tr '\000' '\n' < "$process/cmdline" | grep -Fxq -- main.py || continue
    test "$(readlink -f "$process/cwd")" = "$COMFYUI_PATH" || continue
    application_pid="${process##*/}"
    break
  done
  test -z "$application_pid" || break
  kill -0 "$launcher"
  attempt=$((attempt + 1))
  sleep 1
done

test -n "$application_pid"
test "$(tr '\000' '\n' < "/proc/$application_pid/cmdline" | sed -n '1p')" = \
  "/opt/venv/bin/python"
tr '\000' '\n' < "/proc/$application_pid/cmdline" | grep -Fxq -- main.py
test "$(readlink -f "/proc/$application_pid/cwd")" = "$COMFYUI_PATH"

attempt=0
until curl --fail --silent --show-error \
  http://127.0.0.1:8199/system_stats >/dev/null; do
  kill -0 "$application_pid"
  attempt=$((attempt + 1))
  test "$attempt" -lt 180
  sleep 1
done

test ! -e "$COMFYUI_PATH/.venv"
test ! -L "$COMFYUI_PATH/.venv"
test ! -e "$COMFYUI_PATH/venv"
test ! -L "$COMFYUI_PATH/venv"
"""

_DISABLED_PROBE = r"""
set -eu

test ! -e /opt/uv/tools/comfy-cli
test ! -L /opt/uv/tools/comfy-cli
test ! -e /opt/cdh/build/comfy-cli-inventory.txt
test ! -L /opt/cdh/build/comfy-cli-inventory.txt
for command in comfy comfy-cli comfycli; do
  test ! -e "/opt/uv/bin/$command"
  test ! -L "/opt/uv/bin/$command"
done
(cd "$COMFYUI_PATH" && /opt/venv/bin/python -c 'import comfy')
uv --no-config pip check --python /opt/venv/bin/python --no-python-downloads
"""


def _image(variable: str) -> str:
    image = os.environ.get(variable)
    if not image:
        pytest.skip(f"set {variable} to a locally built acceptance image")
    return image


def _run_disposable(image: str, script: str) -> None:
    name = f"cdh-comfy-cli-smoke-{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--entrypoint",
                "/bin/sh",
                image,
                "-ec",
                script,
            ],
            check=True,
            timeout=300,
        )
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


def test_enabled_image_external_help_and_workspace_python_bridge() -> None:
    _run_disposable(_image(_ENABLED_IMAGE), _ENABLED_PROBE)


def test_disabled_image_has_no_comfy_cli_artifact_and_healthy_application() -> None:
    _run_disposable(_image(_DISABLED_IMAGE), _DISABLED_PROBE)
