#!/usr/bin/env bash

set -euo pipefail

python_version="${1:-}"
case "$python_version" in
    3.12.13 | 3.13.14 | 3.14.6) ;;
    *)
        echo "usage: $0 <3.12.13|3.13.14|3.14.6>" >&2
        exit 2
        ;;
esac

artifact_dir="$(mktemp -d)"
trap 'rm -rf "$artifact_dir"' EXIT
project_root="$(pwd -P)"

uv sync --locked --python "$python_version"
uv run --locked --python "$python_version" ruff format --check .
uv run --locked --python "$python_version" ruff check .
uv run --locked --python "$python_version" pytest
uv run --locked --python "$python_version" \
    python -m build --outdir "$artifact_dir/dist" .

wheel_path="$(find "$artifact_dir/dist" -maxdepth 1 -type f -name '*.whl' -print -quit)"
if [[ -z "$wheel_path" ]]; then
    echo "wheel build did not produce an artifact" >&2
    exit 1
fi

uv venv --python "$python_version" "$artifact_dir/venv"
uv --no-config pip install \
    --python "$artifact_dir/venv/bin/python" \
    "$wheel_path"
"$artifact_dir/venv/bin/python" - "$wheel_path" "$project_root" <<'PY'
from email import policy
from email.parser import BytesParser
from pathlib import Path
import sys
import tomllib
import zipfile

wheel_path = Path(sys.argv[1])
project_root = Path(sys.argv[2])
project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]
with zipfile.ZipFile(wheel_path) as archive:
    metadata_paths = [
        name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
    ]
    license_paths = [
        name for name in archive.namelist() if name.endswith(".dist-info/licenses/LICENSE")
    ]
    if len(metadata_paths) != 1 or len(license_paths) != 1:
        raise SystemExit("wheel metadata or license layout is invalid")
    metadata = BytesParser(policy=policy.default).parsebytes(
        archive.read(metadata_paths[0])
    )
    if metadata.get("Summary") != project["description"]:
        raise SystemExit("wheel summary does not match project description")
    if metadata.get("Description-Content-Type") is not None:
        raise SystemExit("wheel unexpectedly contains a long-description content type")
    if metadata.get_payload().strip():
        raise SystemExit("wheel unexpectedly contains a long-description payload")
    if archive.read(license_paths[0]) != (project_root / "LICENSE").read_bytes():
        raise SystemExit("wheel license does not match the repository license")
PY
mkdir "$artifact_dir/clean-cwd"
(
    cd "$artifact_dir/clean-cwd"
    "$artifact_dir/venv/bin/cdh" --help >/dev/null
    "$artifact_dir/venv/bin/python" - \
        "$artifact_dir/canonical-wheel.whl" \
        "$artifact_dir/canonical-wheel.digest" <<'PY'
from pathlib import Path
import sys

from comfyui_docker_helper.host.release_wheel import build_canonical_wheel

wheel = build_canonical_wheel()
Path(sys.argv[1]).write_bytes(wheel.content)
Path(sys.argv[2]).write_text(wheel.digest, encoding="utf-8")
PY
)
"$artifact_dir/venv/bin/python" - \
    "$wheel_path" \
    "$artifact_dir/canonical-wheel.whl" \
    "$artifact_dir/canonical-wheel.digest" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys

formal = Path(sys.argv[1]).read_bytes()
canonical = Path(sys.argv[2]).read_bytes()
observed_digest = Path(sys.argv[3]).read_text(encoding="utf-8")
expected_digest = f"sha256:{sha256(formal).hexdigest()}"
if canonical != formal or observed_digest != expected_digest:
    raise SystemExit("installed canonical wheel does not match the formal wheel")
PY
