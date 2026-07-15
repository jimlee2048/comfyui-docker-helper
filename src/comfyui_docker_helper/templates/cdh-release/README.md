# ComfyUI Docker Helper

`comfyui-docker-helper` (`cdh`) builds customized ComfyUI Docker Buildx images
from declarative TOML. It manages Python and PyTorch, an official ComfyUI
checkout, optional Manager and comfy-cli installations, custom nodes, files,
and runtime hooks.

One or more configuration layers merge into an effective configuration. cdh
resolves moving inputs into canonical `config.lock.toml` schema v1, constructs
immutable BuildPlan schema v1, and materializes a digest-bound build context:

```text
effective config -> canonical lock v1 -> BuildPlan v1 -> build context
```

Image-internal build helpers consume only generated, digest-bound inputs. They
do not reload host configuration or its lock to make build decisions.

## Requirements

- Python 3.12, 3.13, or 3.14;
- Docker with Buildx for `cdh host build`;
- NVIDIA Docker support with driver `>=580.65.06` on a Turing-or-newer x86_64
  GPU.

CPython 3.13.14 is the default. CPython 3.12.13 and standard-GIL 3.14.6 are
explicitly selectable, tested supported profiles; cdh never switches Python
versions automatically. CUDA 13.0.3, PyTorch 2.12.1, torchvision 0.27.1,
Ubuntu 24.04, and `linux/amd64` complete the default configuration.
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
image_flavor = "cudnn-devel"
image_distro = "ubuntu24.04"

[python]
version = "3.13.14"
uv_version = "0.11.28"
uv_tools = []

[pytorch]
version = "2.12.1"
extra_packages = ["torchvision==0.27.1"]

[comfyui]
version = "0.11.0"
install_cli = true
install_manager = false

[build]
platforms = ["linux/amd64"]
tags = ["my-comfy:dev"]
output = "load"
```

ComfyUI v0.11.0 is the minimum supported release. Every resolved formal,
moving, nightly, or full-commit checkout must descend from the immutable
v0.11.0 floor commit before dependency installation begins.

`install_manager = true` installs the exact checkout's declared Manager package
into `/opt/venv` and verifies its absolute `/opt/venv/bin/cm-cli` capability.
It does not add `--enable-manager` to ComfyUI startup; add that runtime argument
explicitly through `comfyui.extra_args` when the selected checkout supports it.
This application capability is independent of the optional isolated comfy-cli
user tool.

Custom nodes run once in their declared Registry/direct-Git order. Registry
nodes use the verified absolute `cm-cli`, one exact request per process, while
direct-Git nodes pass the declared URL through unchanged as an acquisition
locator and use the locked exact commit as content authority; cdh does not
canonicalize the URL or claim it identifies the actual network endpoint. cdh
verifies the ordered node set around hooks and writes declaration-ordered
evidence to `/opt/cdh/build/custom-node-inventory.json`. Registry installs do
not invoke the optional `comfy`, `comfy-cli`, or `comfycli` commands.
Even with no custom nodes, the same layer validates the custom-node root, writes
the exact empty inventory, and performs the final application dependency check.
The final factual application distribution inventory is written separately to
`/opt/cdh/build/application-inventory.txt`.

See [`examples/full.toml`](examples/full.toml) for the complete current schema.

## Layered host configuration

Repeat `-f/--file` to merge TOML layers in command-line order. Later scalar
values and table fields override earlier values. Custom nodes merge by Registry
ID or direct-Git URL, and files merge by `dir` plus `filename`; a later empty
`custom_nodes = []` or `files = []` resets that collection. Uniqueness and
cross-field rules are checked against the resulting effective configuration,
not against each source file in isolation.

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

Malformed or unsupported lock schemas fail with an actionable
remove-and-regenerate diagnostic.

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
force-replacing an existing executable. When `[comfyui].install_cli=true`, cdh
also resolves the highest target-compatible stable `comfy-cli>=1.7.0`, locks
the exact result, and installs it in `/opt/uv/tools/comfy-cli` after cdh and
before generic tools. The `comfy`, `comfy-cli`, and `comfycli` commands are
linked under `/opt/uv/bin`; cdh does not invoke them during an image build.
Set `install_cli=false` to omit the request, lock identity, tool environment,
links, and verification.
The cdh-owned host resolver uv, release `uv_build` backend, and container
uv/uvx image are independently locked and verified identities even when their
current versions are equal.

Users may mutate the public uv tool store at runtime, but those changes are
outside the baked-image replay contract. Updating baked cdh or configured tools
requires the corresponding config/lock change where applicable and an image
rebuild; `uv tool upgrade --all` is not an image update mechanism.

## Runtime configuration

Each image contains generated runtime settings at
`/opt/cdh/runtime/config.toml`. Mount an optional
`/etc/cdh/runtime/config.toml` to change runtime-only settings without
rebuilding. Values are applied in this order, with later sources taking
precedence:

```text
built-in defaults < baked config < mounted config < environment
```

Runtime configuration accepts ComfyUI `listen`, `port`, and `extra_args`; cdh
downloader settings; `system.ssh`; and `files`. Recognized host-only settings
are ignored with a warning when they appear in runtime TOML. Unknown or
otherwise unsupported runtime fields are rejected. For example:

```toml
[comfyui]
listen = "0.0.0.0"
port = 8188
extra_args = ["--preview-method", "auto"]

[cdh]
default_downloader = "aria2"
default_download_mode = "sync"
download_max_attempts = 3
download_failure_policy = "continue"

[system.ssh]
enable = false
port = 22
```

The environment overrides are:

- `CDH_COMFYUI_LISTEN`, `CDH_COMFYUI_PORT`, and
  `CDH_COMFYUI_EXTRA_ARGS`;
- `CDH_DEFAULT_DOWNLOADER`, `CDH_DEFAULT_DOWNLOAD_MODE`,
  `CDH_DOWNLOAD_MAX_ATTEMPTS`, and `CDH_DOWNLOAD_FAILURE_POLICY`; and
- `SSH_ENABLE`, `SSH_PORT`, `SSH_PASSWORD`, and `SSH_PUB_KEY`.

`CDH_COMFYUI_EXTRA_ARGS` uses POSIX shell-style word parsing without executing
a shell. `SSH_PUB_KEY` appends one normalized public key to the configured key
set.

## Files, downloads, and persistent state

Host `[[files]]` declarations are projected into the generated build phase and
the baked runtime configuration. At startup, baked and mounted file lists merge
by `dir` plus `filename`; a mounted `files = []` clears the effective runtime
list. Every target is relative to `COMFYUI_PATH`.

Synchronous runtime downloads finish before pre-start hooks. Asynchronous
downloads are accepted into the background queue before ComfyUI starts and may
continue while it runs. `download_max_attempts` bounds the outer attempts for
each build or runtime file, and HTTPX `retries` controls retries within each
outer HTTPX attempt. `download_failure_policy` applies in both contexts: `fail`
stops the build helper after an exhausted build file, while `continue` reports
the failure and tries subsequent files. For synchronous runtime files, `fail`
aborts startup after exhaustion and `continue` tries subsequent items. For
asynchronous runtime files, `fail` stops the remaining queue without stopping
ComfyUI, while `continue` tries subsequent queued items.

Runtime reconciliation is persisted at `/var/lib/cdh/runtime/state.json`.
In-progress transfer data lives in a target-local `.cdh-staging` directory so
completed targets can be replaced safely. Mount `/var/lib/cdh/runtime`
separately to preserve reconciliation state, and mount each desired target
directory to preserve downloaded files. The state file is cdh-owned internal
state, not user configuration; do not edit it.

## SSH and confidential values

SSH is disabled by default. Enable it with runtime TOML or `SSH_ENABLE=true`
and provide at least one public key or password, preferably through
`SSH_PUB_KEY` or `SSH_PASSWORD` at container startup. If SSH is enabled without
an effective credential, cdh warns and does not start sshd. The entrypoint
starts, monitors, and stops the foreground sshd process with the rest of the
container lifecycle.

Prefer runtime injection for confidential values. Ordinary TOML values, URLs,
environment variables, rendered files, image history, and logs can expose
their contents when their contract requires it. cdh keeps its own ephemeral
credentials internal and avoids printing the explicit SSH password, but it
does not infer that arbitrary configured values are secrets.

## Runtime lifecycle and hooks

Pass `--hooks-dir <dir>` to `cdh host render` or `cdh host build` to bake a
complete runtime hook tree. When the option is omitted, an existing `./hooks`
tree is used. The root may contain only `pre-start.d/`, `post-start.d/`, and
`stop.d/`; files must be regular `.sh` or `.py` files. Symlinks,
special files, nested directories, and unknown entries are rejected.

Every baked hook is verified while materializing and copied to
`/opt/cdh/runtime/hooks`. Mounted `/etc/cdh/runtime/hooks` remains an external
runtime input. Baked hooks run before mounted hooks and filenames run in lexical
order within each phase.

Startup completes synchronous downloads, runs pre-start hooks, starts sshd when
enabled, accepts asynchronous downloads, and then starts ComfyUI. If post-start
hooks exist, cdh waits for ComfyUI readiness before running them.

On the first `SIGTERM` or `SIGINT`, cdh stops the asynchronous download queue
and sshd, runs stop hooks while ComfyUI remains alive, then forwards the signal
to ComfyUI and waits for it for a bounded interval. A second signal cancels the
remaining stop hooks. The container runtime's stop timeout is independent and
may still terminate the container if its own deadline expires.

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

- Duplicate, uniqueness, and cross-field checks apply after all host layers
  have merged. This includes platforms, Python and PyTorch package
  declarations, uv tools, Registry IDs, direct-Git URLs and targets, and file
  targets.
- `[build].platforms` is ordered, non-empty, duplicate-free, and currently
  accepts exactly `["linux/amd64"]`.
- CUDA version is the sole inference-backend version authority and solely
  derives the PyTorch channel. `image_flavor` accepts `base`, `runtime`,
  `devel`, `cudnn-runtime`, or `cudnn-devel` (default), while `image_distro`
  accepts `ubuntu22.04` or `ubuntu24.04` (default). These selectors construct
  the exact NVIDIA image tag without changing the PyTorch channel. Validation
  is structural and offline; a missing upstream tag or `linux/amd64`
  descriptor fails during provider resolution without fallback.
  Resolving a `base` or `runtime` image does not promise development headers or
  cuDNN capabilities; dependent install, build, or runtime gates may still fail
  without fallback.
- Direct Python declarations accept bare names, exact stable versions, or
  supported bounded selectors. URL/VCS/local/editable/raw-option forms and
  environment markers are rejected.
- `[python].uv_tools` installs standalone CLI distributions into isolated tool
  environments. Duplicate normalized owners and executable collisions,
  including `cdh` and the optional comfy-cli commands, fail the image build.
  `comfy-cli` itself is reserved to `[comfyui].install_cli` across Python
  extras, PyTorch extras, and uv tools in both modes.
- `[system].env` defines non-managed runtime image values. Names beginning with
  `UV_` or `PIP_` are reserved so config cannot alter build-time package
  sources, constraints, configuration, Python selection, or tool ownership.
  Runtime `docker run -e` overrides are outside baked-image replay guarantees.
- Registry custom-node IDs must be valid Python project names. They require
  Manager, preserve their raw locked ID/version,
  and reject duplicate normalized IDs. Direct-Git nodes are independently
  locked to full commits and preserve their declared acquisition URLs.
- HTTPX `retries` sets the retries within each outer HTTPX download attempt.
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
