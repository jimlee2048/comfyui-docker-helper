# ComfyUI Docker Helper

`comfyui-docker-helper` (`cdh`) validates one strict configuration, resolves
moving inputs into canonical `config.lock.toml` schema v1, constructs immutable
BuildPlan schema v1, and materializes a Docker Buildx context.

The active planning chain is:

```text
validated final config -> canonical lock v1 -> BuildPlan v1 -> phase inputs
```

Container build helpers consume only digest-bound phase inputs. They do not
reload the root config or lock to make planning decisions.

## Requirements

- Python 3.12 or 3.13;
- Docker with Buildx for `cdh host build`;
- NVIDIA Docker support for CUDA images.

The tested v0.5 planning baseline is CPython 3.13.14 (with 3.12.13 fallback),
CUDA 13.0.3, PyTorch 2.12.1, torchvision 0.27.1, Ubuntu 24.04, and
`linux/amd64`. CUDA 13.0.3 derives the internal PyTorch channel `cu130`.

## Install

```bash
uv tool install comfyui-docker-helper
cdh --help
```

## Minimal configuration

```toml
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "13.0.3"

[python]
version = "3.13.14"
uv_version = "0.11.28"

[pytorch]
version = "2.12.1"
extra_packages = ["torchvision==0.27.1"]

[comfyui]
version = "0.4.0"
cli_version = "1.5.3"
install_manager = false

[build]
platforms = ["linux/amd64"]
tags = ["my-comfy:dev"]
output = "load"
```

See [`examples/full.toml`](examples/full.toml) for all currently active fields.

## Validate, render, and build

Validation is local and offline: it performs no provider calls, Docker calls,
or writes.

```bash
cdh host validate -f examples/minimal.toml
```

Render a context and its canonical lock:

```bash
cdh host render \
  -f examples/minimal.toml \
  -o .cdh/build/current \
  --overwrite
```

Buildx receives the sole configured target platform explicitly:

```bash
cdh host build \
  -f examples/minimal.toml \
  --context-dir .cdh/build/current \
  -t my-comfy:dev \
  --load
```

## Lock and no-write modes

- Default reconciliation reuses unchanged entries, resolves only missing or
  changed requests, removes deleted identities, and writes atomically.
- `--locked` performs zero provider calls and zero writes while requiring an
  exact compatible canonical lock and local input set.
- `--upgrade-lock` refreshes moving selectors while retaining unchanged exact
  selections.
- `--check` computes default reconciliation and compares the expected context
  without writing.
- `--dry-run` applies the selected resolution policy, prints the exact
  BuildPlan preview, and performs no writes or build.

Old, malformed, or future lock schemas fail with one remove-and-regenerate
diagnostic. There is no compatibility reader or migration path.

PyTorch configuration versions are selectors, not resolved artifact versions.
The canonical PyTorch request binds the CUDA-derived channel, index URL, target
Python/platform, and complete package group into its `request_digest`. The lock
records each complete resolved distribution version, including a stable local
label such as `2.12.1+cu130`; BuildPlan carries that same exact install version.
Resolved versions never enter `request_digest`, and the resolved version is not
split into separate public/local fields.

## Runtime lifecycle hooks

Pass `--hooks-dir <dir>` to `cdh host render` or `cdh host build` to bake a
complete runtime hook tree. When the option is omitted, an existing `./hooks`
tree is used. The root may contain only `pre-start.d/`, `post-start.d/`, and
`stop.d/`; files must be regular `.sh` or `.py` files. Symlinks,
special files, nested directories, and unknown entries are rejected.

Every baked hook is content-locked under a runtime-hook identity, bound into
BuildPlan, verified while materializing, and copied to
`/opt/cdh/runtime/hooks`. Mounted `/etc/cdh/runtime/hooks` remains an external
runtime input. Baked hooks run before mounted hooks and filenames run in lexical
order within pre-start, post-start, and stop phase order.

## Rendered context

A rendered context contains:

- `config.lock.toml`, used only by the host for later reconciliation;
- `build-plan.json` and `manifest-binding.json`;
- digest-bound JSON documents under `phases/`;
- verified referenced hook bytes under `inputs/`, when configured; and
- a BuildPlan-derived `runtime/config.toml` plus content-locked `runtime/hooks`
  when configured, copied to the paths consumed by the entrypoint; and
- a Dockerfile whose `FROM` values are literal `tag@sha256` references.

The context does not contain a root `config.toml`, and the Dockerfile has no ARG
that can override lock-authoritative image identities. Host-local source paths
and resolver `request_digest` values are excluded from BuildPlan and phase
documents.

## Configuration boundaries

- `[build].platforms` is ordered, non-empty, duplicate-free, and currently
  accepts exactly `["linux/amd64"]`.
- CUDA version is the sole inference-backend version authority. There is no
  independent wheel-channel, image-flavor, or distro selector.
- Direct Python declarations accept bare names, exact stable versions, or
  supported bounded selectors. URL/VCS/local/editable/raw-option forms and
  environment markers are rejected.
- Registry custom nodes require Manager. Direct Git nodes are independently
  locked to full commits.
- HTTPX `retries` remains an active public setting.
- Ordinary configured values may appear in rendered artifacts or logs when the
  contract requires them. Keep confidential values out of ordinary config
  fields unless that exposure is acceptable.

## Development

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pytest tests/unit tests/integration
uv build
```
