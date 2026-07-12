#!/usr/bin/env bash

set -euo pipefail

python_version="${1:-}"
case "$python_version" in
    3.12 | 3.13) ;;
    *)
        echo "usage: $0 <3.12|3.13>" >&2
        exit 2
        ;;
esac

artifact_dir="$(mktemp -d)"
trap 'rm -rf "$artifact_dir"' EXIT

uv sync --locked --python "$python_version"
uv run --locked --python "$python_version" ruff format --check .
uv run --locked --python "$python_version" ruff check .
uv run --locked --python "$python_version" pytest tests/unit tests/integration
uv build --python "$python_version" --out-dir "$artifact_dir/dist"
uv export \
    --locked \
    --python "$python_version" \
    --no-dev \
    --no-emit-project \
    --format requirements.txt \
    --output-file "$artifact_dir/production-requirements.txt" \
    >/dev/null

wheel_path="$(find "$artifact_dir/dist" -maxdepth 1 -type f -name '*.whl' -print -quit)"
if [[ -z "$wheel_path" ]]; then
    echo "wheel build did not produce an artifact" >&2
    exit 1
fi

uv venv --python "$python_version" "$artifact_dir/venv"
uv --no-config pip install \
    --python "$artifact_dir/venv/bin/python" \
    --require-hashes \
    --no-deps \
    --requirements "$artifact_dir/production-requirements.txt"
uv --no-config pip install \
    --python "$artifact_dir/venv/bin/python" \
    --no-deps \
    "$wheel_path"
"$artifact_dir/venv/bin/cdh" --help >/dev/null
