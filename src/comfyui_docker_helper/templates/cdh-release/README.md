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
- NVIDIA Docker support with driver `>=580.65.06` on a Turing-or-newer x86_64
  GPU.

The tested v0.5 planning baseline is CPython 3.13.14 (with 3.12.13 fallback),
CUDA 13.0.3, PyTorch 2.12.1, torchvision 0.27.1, Ubuntu 24.04, and
`linux/amd64`. CUDA 13.0.3 derives the internal PyTorch channel `cu130`.
The default resolved inference group is installed as the complete exact
distributions `torch==2.12.1+cu130` and `torchvision==0.27.1+cu130` from the
derived cu130 index.

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
uv_tools = []

[pytorch]
version = "2.12.1"
extra_packages = ["torchvision==0.27.1"]

[comfyui]
version = "0.11.0"
cli_version = "1.5.3"
install_manager = false

[build]
platforms = ["linux/amd64"]
tags = ["my-comfy:dev"]
output = "load"
```

ComfyUI v0.11.0 is the minimum supported release. Every resolved formal,
moving, nightly, or full-commit checkout must descend from the immutable
v0.11.0 floor commit before dependency installation begins.

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
The canonical PyTorch request binds the CUDA-derived channel, both the ordinary
Python index and derived PyTorch index, target Python/platform, and complete
package group into its `request_digest`. The lock
records each complete resolved distribution version, including a stable local
label such as `2.12.1+cu130`; BuildPlan carries that same exact install version.
Resolved versions never enter `request_digest`, and the resolved version is not
split into separate public/local fields.

Every direct member—`torch` plus all `[pytorch].extra_packages`—is mapped
exclusively to the derived PyTorch index. Generic transitive dependencies use
only `[python].index_url`; a missing direct member does not fall back to a
same-named package on the Python index. The complete group is installed
together into `/opt/venv` and verified with that environment's interpreter.
Its exact direct distributions and the setuptools compatibility range derived
from the selected torch wheel metadata are then protected by the root-owned,
read-only
`/opt/cdh/build/python-package-constraints.txt`. cdh scopes that artifact only
to later application installation operations; it is not a global image
`UV_CONSTRAINT` or `PIP_CONSTRAINT`.

The application environment contains exact `pip==26.1.2`. cdh does not
preinstall, lock, or version-gate wheel; packages that need wheel as a runtime
library or CLI must declare it, while source builds declare build dependencies
through their own PEP 517 build metadata. Setuptools has no global exact pin:
its installed version must satisfy the selected torch wheel's derived range
and the final dependency check.

The image installs the exact managed interpreter once, creates the application
environment at `/opt/venv`, and keeps cdh plus configured standalone CLI tools
in isolated uv environments. `UV_TOOL_DIR=/opt/uv/tools`,
`UV_TOOL_BIN_DIR=/opt/uv/bin`, and `/opt/uv/bin` precedes `/opt/venv/bin` on
`PATH`. cdh is installed first from a projected non-editable wheel using the
production-only frozen closure derived from this repository's `uv.lock`.
Each `[python].uv_tools` entry accepts the same bounded direct-requirement
grammar, resolves independently, and installs an exact direct result without
force-replacing an existing executable. comfy-cli and cm-cli remain application
environment tools rather than uv tools.
The cdh-owned host resolver uv, release `uv_build` backend, and container
uv/uvx image are independently locked and verified identities even when their
current versions are equal.

Users may mutate the public uv tool store at runtime, but those changes are
outside the baked-image replay contract. Updating baked cdh or configured tools
requires the corresponding config/lock change where applicable and an image
rebuild; `uv tool upgrade --all` is not an image update mechanism.

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
- `[python].uv_tools` installs standalone CLI distributions into isolated tool
  environments. Duplicate normalized owners and executable collisions,
  including `cdh`, fail the image build.
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
