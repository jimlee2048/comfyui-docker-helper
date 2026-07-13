"""Opt-in disposable-image acceptance for checkout-owned Manager capability."""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.docker, pytest.mark.slow]

_ENABLED_IMAGE = "CDH_MANAGER_ENABLED_IMAGE"
_DISABLED_IMAGE = "CDH_MANAGER_DISABLED_IMAGE"

_BOUNDED_APPLICATION_CLEANUP = r"""
application_is_non_zombie() {
  test -r "/proc/$application_pid/stat" || return 1
  application_state="$(
    sed -n 's/^.*) \([^ ]\).*/\1/p' "/proc/$application_pid/stat"
  )" || return 1
  test -n "$application_state" && \
    test "$application_state" != Z && \
    test "$application_state" != X
}

signal_application() {
  kill "-$1" "$application_pid" 2>/dev/null || true
}

reap_application() {
  wait "$application_pid" 2>/dev/null || true
}

bounded_reap_application() {
  term_attempt_limit="${1:-10}"
  kill_attempt_limit="${2:-5}"
  signal_application TERM
  cleanup_attempt=0
  while application_is_non_zombie && \
    test "$cleanup_attempt" -lt "$term_attempt_limit"; do
    cleanup_attempt=$((cleanup_attempt + 1))
    sleep 1
  done
  if application_is_non_zombie; then
    signal_application KILL
    cleanup_attempt=0
    while application_is_non_zombie && \
      test "$cleanup_attempt" -lt "$kill_attempt_limit"; do
      cleanup_attempt=$((cleanup_attempt + 1))
      sleep 1
    done
  fi
  if application_is_non_zombie; then
    return 1
  fi
  reap_application
  return 0
}

cleanup_application() {
  cleanup_status="$?"
  trap - EXIT INT TERM
  cleanup_failure=0
  bounded_reap_application || cleanup_failure="$?"
  if test "$cleanup_status" -eq 0 && test "$cleanup_failure" -ne 0; then
    cleanup_status="$cleanup_failure"
  fi
  exit "$cleanup_status"
}

complete_application_probe() {
  cleanup_status=0
  bounded_reap_application "${1:-10}" "${2:-5}" || cleanup_status="$?"
  trap - EXIT INT TERM
  return "$cleanup_status"
}

trap cleanup_application EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"""

_ENABLED_PROBE = (
    r"""
set -eu

test ! -e /opt/uv/tools/comfy-cli
test ! -L /opt/uv/tools/comfy-cli
for command in comfy comfy-cli comfycli; do
  test ! -e "/opt/uv/bin/$command"
  test ! -L "/opt/uv/bin/$command"
done

test -x /opt/venv/bin/cm-cli
test "$(stat -c '%u:%g' /opt/venv/bin/cm-cli)" = 0:0
test "$(sed -n '1p' /opt/venv/bin/cm-cli)" = '#!/opt/venv/bin/python'
anchor=/opt/venv/lib/python3.13/site-packages/comfyui-docker-helper-comfyui.pth
test -f "$anchor"
test ! -L "$anchor"
test "$(stat -c '%a' "$anchor")" = 444
test "$(stat -c '%u:%g' "$anchor")" = 0:0
test "$(cat "$anchor")" = "$COMFYUI_PATH"

/opt/venv/bin/python -I -c '
import importlib
import importlib.metadata as metadata
import importlib.util
import json
import os
import pathlib
import sys

plan = json.loads(pathlib.Path("/opt/cdh/build/build-plan.json").read_text())
manager = plan["application"]["comfyui"]["manager"]
assert manager is not None
assert manager["requirements_path"] == "manager_requirements.txt"
assert manager["distribution"] == "comfyui-manager"
assert manager["import_name"] == "comfyui_manager"
assert manager["executable"] == "/opt/venv/bin/cm-cli"
assert manager["entrypoint_name"] == "cm-cli"
assert manager["import_anchor"] == (
    "/opt/venv/lib/python3.13/site-packages/"
    "comfyui-docker-helper-comfyui.pth"
)
assert metadata.version("comfyui-manager") == "4.0.5"
assert pathlib.Path(sys.prefix) == pathlib.Path("/opt/venv")
workspace = pathlib.Path(os.environ["COMFYUI_PATH"]).resolve(strict=True)
assert workspace in tuple(pathlib.Path(item).resolve() for item in sys.path if item)
folder_paths = importlib.util.find_spec("folder_paths")
assert folder_paths is not None and folder_paths.origin is not None
assert pathlib.Path(folder_paths.origin).resolve().is_relative_to(workspace)
site_packages = pathlib.Path(manager["import_anchor"]).parent.resolve(strict=True)
manager_name = manager["import_name"]
manager_root = (
    site_packages / pathlib.Path(*manager_name.split("."))
).resolve(strict=True)
assert manager_root.is_relative_to(site_packages)
manager_spec = importlib.util.find_spec(manager_name)
assert manager_spec is not None and manager_spec.name == manager_name
manager_search = manager_spec.submodule_search_locations
assert manager_search is not None
manager_locations = tuple(
    pathlib.Path(item).resolve(strict=True) for item in manager_search
)
assert manager_locations and all(item == manager_root for item in manager_locations)
manager_origin = (
    None
    if manager_spec.origin is None
    else pathlib.Path(manager_spec.origin).resolve(strict=True)
)
assert manager_origin is None or manager_origin.is_relative_to(manager_root)
comfy_name = "comfy"
comfy_root = (workspace / comfy_name).resolve(strict=True)
assert comfy_root.is_relative_to(workspace)
comfy_spec = importlib.util.find_spec(comfy_name)
assert comfy_spec is not None and comfy_spec.name == comfy_name
comfy_search = comfy_spec.submodule_search_locations
assert comfy_search is not None
comfy_locations = tuple(
    pathlib.Path(item).resolve(strict=True) for item in comfy_search
)
assert comfy_locations and all(item == comfy_root for item in comfy_locations)
comfy_origin = (
    None
    if comfy_spec.origin is None
    else pathlib.Path(comfy_spec.origin).resolve(strict=True)
)
assert comfy_origin is None or comfy_origin.is_relative_to(comfy_root)
module = importlib.import_module(manager_name)
imported_spec = module.__spec__
assert imported_spec is not None and imported_spec.name == manager_spec.name
imported_search = imported_spec.submodule_search_locations
assert imported_search is not None
imported_locations = tuple(
    pathlib.Path(item).resolve(strict=True) for item in imported_search
)
assert imported_locations == manager_locations
imported_origin = (
    None
    if imported_spec.origin is None
    else pathlib.Path(imported_spec.origin).resolve(strict=True)
)
assert imported_origin == manager_origin
comfy = importlib.import_module(comfy_name)
imported_comfy_spec = comfy.__spec__
assert imported_comfy_spec is not None
assert imported_comfy_spec.name == comfy_spec.name
imported_comfy_search = imported_comfy_spec.submodule_search_locations
assert imported_comfy_search is not None
imported_comfy_locations = tuple(
    pathlib.Path(item).resolve(strict=True) for item in imported_comfy_search
)
assert imported_comfy_locations == comfy_locations
imported_comfy_origin = (
    None
    if imported_comfy_spec.origin is None
    else pathlib.Path(imported_comfy_spec.origin).resolve(strict=True)
)
assert imported_comfy_origin == comfy_origin
owners = [
    distribution.metadata["Name"]
    for distribution in metadata.distributions()
    for item in distribution.entry_points
    if item.group == "console_scripts" and item.name == "cm-cli"
]
assert owners == ["comfyui-manager"]
assert "--enable-manager" not in plan["runtime"]["launch_command"]
'
uv --no-config pip check --python /opt/venv/bin/python --no-python-downloads

cd "$COMFYUI_PATH"
/opt/venv/bin/python main.py \
  --listen 127.0.0.1 \
  --port 8201 \
  --disable-auto-launch \
  --cpu \
  --enable-manager &
application_pid="$!"
"""
    + _BOUNDED_APPLICATION_CLEANUP
    + r"""
attempt=0
until curl --fail --silent --show-error \
  http://127.0.0.1:8201/system_stats >/dev/null; do
  kill -0 "$application_pid"
  attempt=$((attempt + 1))
  test "$attempt" -lt 180
  sleep 1
done

complete_application_probe
"""
)

_DISABLED_PROBE = r"""
set -eu

test ! -e /opt/venv/bin/cm-cli
test ! -L /opt/venv/bin/cm-cli
anchor=/opt/venv/lib/python3.13/site-packages/comfyui-docker-helper-comfyui.pth
test ! -e "$anchor"
test ! -L "$anchor"
(cd "$COMFYUI_PATH" && /opt/venv/bin/python -c '
import importlib.metadata as metadata
import importlib.util
import json
import pathlib

plan = json.loads(pathlib.Path("/opt/cdh/build/build-plan.json").read_text())
assert plan["application"]["comfyui"]["manager"] is None
try:
    metadata.version("comfyui-manager")
except metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError("Manager distribution exists while disabled")
assert importlib.util.find_spec("comfyui_manager") is None
import comfy
')
uv --no-config pip check --python /opt/venv/bin/python --no-python-downloads
"""


def _image(variable: str) -> str:
    image = os.environ.get(variable)
    if not image:
        pytest.skip(f"set {variable} to a locally built acceptance image")
    return image


def _run_disposable(image: str, script: str) -> None:
    name = f"cdh-manager-smoke-{uuid.uuid4().hex[:12]}"
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


def test_enabled_image_has_checkout_owned_manager_capability() -> None:
    _run_disposable(_image(_ENABLED_IMAGE), _ENABLED_PROBE)


def test_disabled_image_preserves_manager_absence_and_application_health() -> None:
    _run_disposable(_image(_DISABLED_IMAGE), _DISABLED_PROBE)
