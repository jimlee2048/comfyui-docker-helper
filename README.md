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
- for GPU execution on x86_64, NVIDIA Container Toolkit support, driver
  `>=580.65.06`, and a Turing-or-newer NVIDIA GPU.

CPython 3.13.14 is the default. CPython 3.12.13 and standard-GIL 3.14.6 are
explicitly selectable, tested supported profiles; cdh never switches Python
versions automatically. CUDA 13.0.3, PyTorch 2.12.1, torchvision 0.27.1,
Ubuntu 24.04, and `linux/amd64` complete the default configuration.
The default formal-baseline inference group resolves to the complete exact
distributions `torch==2.12.1+cu130`, `torchvision==0.27.1+cu130`, and
`torchaudio==2.11.0+cu130` from the derived cu130 index.

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
verifies the ordered node set around hooks. After every build mutation, final
manifest emission re-proves each direct-Git checkout and the exact Registry
identity set from the final filesystem, then records declaration-ordered typed
evidence in `/opt/cdh/build/manifest.json`. Registry installs do
not invoke the optional `comfy`, `comfy-cli`, or `comfycli` commands.
Even with no custom nodes, final observation validates the custom-node root and
records exact empty evidence. The same manifest records the factual final
application and optional isolated comfy-cli distribution inventories.

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

Every direct member—configured `torch`, all `[pytorch].extra_packages`, and
every target-active protected requirement from the exact ComfyUI checkout—is
mapped exclusively to the derived PyTorch index. Generic transitive
dependencies use only `[python].index_url`; a missing direct member does not
fall back to a same-named package on the Python index. The complete group is
installed together into `/opt/venv` and verified with that environment's
interpreter.
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
`PATH`. cdh is installed first from one canonical non-editable wheel rebuilt
from the installed package's release resources. The host validates that wheel
once, binds the same bytes into the image build, and lets standard package
metadata resolve its transitive dependencies from `[python].index_url`.
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

All other cdh-controlled Python resolution and installation uses only
`[python].index_url`, including application extras, ordinary ComfyUI and
Manager requirements, cdh dependencies, optional comfy-cli, generic uv
tools, and cdh-invoked custom-node requirements. Manager/Registry installers
and direct-Git `install.py` remain trusted opaque code; cdh does not claim
network-level source isolation for their arbitrary effects.

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
shutdown_timeout = 8

[system.ssh]
enable = false
port = 22
```

The environment overrides are:

- `CDH_COMFYUI_LISTEN`, `CDH_COMFYUI_PORT`, and
  `CDH_COMFYUI_EXTRA_ARGS`;
- `CDH_DEFAULT_DOWNLOADER`, `CDH_DEFAULT_DOWNLOAD_MODE`,
  `CDH_DOWNLOAD_MAX_ATTEMPTS`, `CDH_DOWNLOAD_FAILURE_POLICY`, and
  `CDH_SHUTDOWN_TIMEOUT`; and
- `SSH_ENABLE`, `SSH_PORT`, `SSH_PASSWORD`, and `SSH_PUB_KEY`.

`CDH_COMFYUI_EXTRA_ARGS` uses POSIX shell-style word parsing without executing
a shell. `SSH_PUB_KEY` appends one normalized public key to the configured key
set.

## Files, downloads, and persistent state

Host `[[files]]` declarations are projected into the generated build phase and
the baked runtime configuration. At startup, baked and mounted file lists merge
by `dir` plus `filename`; a mounted `files = []` clears the effective runtime
list. Every target is relative to `COMFYUI_PATH`. An optional
`checksum = "sha256:<64 hexadecimal digits>"` declares trusted content identity.
Obtain that digest from a source independent enough for your threat model; cdh
does not fetch or infer a digest from the download origin.

Synchronous runtime downloads finish before pre-start hooks. Asynchronous
downloads are accepted into the background queue before ComfyUI starts and may
continue while it runs. `download_max_attempts` is the total transport-attempt
budget for each build or runtime file, including the first attempt. Every
build-time file is required, so an exhausted or terminal build transfer always
fails the Docker build;
`download_failure_policy` applies only at runtime. For synchronous runtime
files, `fail` aborts startup after exhaustion and `continue` tries subsequent
items. For asynchronous runtime files, `fail` stops the remaining queue without
stopping ComfyUI, while `continue` tries subsequent queued items.

Only aria2 may resume from exact cdh-owned staging and control state. One
backend invocation counts as one attempt. If an admitted resumed transfer is
rejected, cdh safely removes only the exact owned partial state and, when the
budget remains, spends the next attempt on one clean aria2 transfer without
backoff. It does not hide a retry or switch backends; HTTPX does not resume.
Containment, symlink or special-file, identity, permission, persistence, and
durability failures always fail closed and are never converted by the runtime
failure policy.

With a checksum, an existing matching regular file is verified and kept
regardless of `overwrite`. A mismatch is preserved and fails when `overwrite`
is false; when true, only a fully downloaded and verified replacement is
atomically placed. Without a checksum, an existing file is skipped as
unverified when `overwrite` is false, while a successful atomic replacement is
transport-complete but does not claim content authenticity when it is true.

Runtime reconciliation is persisted at `/var/lib/cdh/runtime/state.json`.
In-progress transfer data lives in a deterministic target-local `.cdh-staging`
path so completed targets remain untouched until durable atomic replacement.
Mount `/var/lib/cdh/runtime`
separately to preserve reconciliation state, and mount each desired target
directory to preserve downloaded files. The state file is cdh-owned internal
state, not user configuration; do not edit it.

## SSH and confidential values

SSH is disabled by default. Enable it with runtime TOML or `SSH_ENABLE=true`
and provide at least one public key or password, preferably through
`SSH_PUB_KEY` or `SSH_PASSWORD` at container startup. If SSH is enabled without
an effective credential, cdh warns and does not start sshd. The entrypoint
prepares credentials and host keys through bounded cancellable child processes,
then starts, monitors, and stops foreground sshd with the rest of the container
lifecycle.
Package-generated SSH host keys are removed during the image build. When SSH is
enabled, the container generates its own host keys during startup instead of
shipping a shared baked identity.

Prefer runtime injection for confidential values. Ordinary TOML values, URLs,
environment variables, rendered files, image history, and logs can expose
their contents when their contract requires it. cdh keeps its own ephemeral
credentials internal and avoids printing the explicit SSH password, but it
does not infer that arbitrary configured values are secrets.

When custom-node configuration references `pre_install_hooks` or
`post_install_hooks`, pass `--build-hooks-dir <dir>` to validate, render, and
build. The option has no default and is ignored when no build hook is
referenced. Paths are relative to the explicit root; cdh preserves any safe
user-defined subdirectory structure and admits only referenced regular files.
Referenced build-hook source is copied to `/opt/cdh/build/hooks` as durable
verified evidence and remains in the final image and its committed layers. Do
not place secrets in these hook files.

## Runtime lifecycle and hooks

Pass `--runtime-hooks-dir <dir>` to `cdh host render` or `cdh host build` to
bake a complete runtime hook tree. When the option is omitted, no baked runtime
hooks are planned. The root may contain only `pre-start.d/`, `post-start.d/`,
and `stop.d/`; files must be regular `.sh` or `.py` files. Symlinks, special
files, nested directories, and unknown entries are rejected.

Every baked hook is verified while materializing and copied to
`/opt/cdh/runtime/hooks`. Mounted `/etc/cdh/runtime/hooks` remains an external
runtime input. Baked hooks run before mounted hooks and filenames run in lexical
order within each phase.

Startup completes synchronous downloads, runs pre-start hooks, starts sshd when
enabled, accepts asynchronous downloads, and then starts ComfyUI. If post-start
hooks exist, cdh waits for ComfyUI readiness before running them.

Each hook runs as an isolated session while its leader is active. cdh owns that
active execution's timeout, cancellation, process group, result, and leader
reap. After a hook leader finishes, cdh does not discover, supervise, or signal
background processes that the trusted script deliberately left running. If a
startup hook launches a background service, pair it with a stop hook that uses
the service's control interface, or a carefully validated PID file, to request
termination and wait for exit. Service control is preferred; stale PID files,
PID reuse, and process-identity checks remain the hook author's responsibility.

On the first `SIGTERM` or `SIGINT`, cdh promptly starts cancellation of the
asynchronous download queue and sshd, then runs ordered stop hooks while
ComfyUI remains alive. `shutdown_timeout` is one total monotonic budget for
this signal path. Its default is eight seconds, with the final two seconds
reserved for forwarding the original signal to ComfyUI and reaping managed
children. When the earlier hook portion expires, cdh terminates the active hook
group and skips later hooks; at the total deadline, it force-stops only managed
children that remain alive. A second `SIGTERM` or `SIGINT` skips any remaining
grace period and immediately force-stops the active hook, downloader, sshd, and
ComfyUI. The first signal remains the shutdown identity; a force-killed ComfyUI
normally makes the container exit with 137. A natural ComfyUI exit keeps its
own exit code, performs component cleanup, and does not run signal-only stop
hooks.

Every rendered image runs Tini as PID 1 with cdh as its direct child. Tini
forwards Docker's signal to cdh and reaps orphaned zombies adopted by PID 1; it
is not a service supervisor and does not provide health checks or graceful
shutdown for hook-started services. Such a service has no graceful-shutdown
guarantee when its stop hook is missing or fails, ComfyUI exits naturally,
Docker escalates to `SIGKILL`, or PID 1 exits early. PID-namespace teardown
terminates remaining processes but is not graceful shutdown.

Docker or another orchestrator owns a separate external hard limit. Linux
`docker stop` and Compose normally allow ten seconds before `SIGKILL`; cdh's
eight-second default leaves only a best-effort scheduling margin and cannot
discover or override that external value. For a custom deployment, configure
Docker `--stop-timeout` or Compose `stop_grace_period` greater than cdh's total.
For example, long hooks can use `shutdown_timeout = 55` with an external grace
greater than 55 seconds. Setting cdh to `-1` disables only its outer and hook
deadlines; cdh-owned component operations remain bounded, Docker's own `-1` is
independent, and no cleanup can continue after external `SIGKILL`.

## Rendered context

Key rendered-context artifacts include:

- `config.lock.toml`, used only by the host for later reconciliation;
- one digest-bound canonical `build-plan.json`, used by build-time helpers;
- one host-validated canonical cdh wheel under `bootstrap/` for a read-only
  BuildKit bind mount;
- verified referenced build-hook bytes under `build/hooks/`, when configured;
- a BuildPlan-derived `runtime/config.toml` plus content-locked `runtime/hooks`
  when configured, copied to the paths consumed by the entrypoint; and
- a Dockerfile whose `FROM` values are literal `tag@sha256` references and
  whose explicit `STOPSIGNAL SIGTERM` precedes the absolute exec-form
  `/usr/bin/tini -- /opt/uv/bin/cdh container entrypoint` launch contract.

The context does not contain a root `config.toml`, and the Dockerfile has no ARG
that can override lock-authoritative image identities. Host-local source paths
and resolver `request_digest` values are excluded from the BuildPlan. Its
`.dockerignore` excludes only host reconciliation state: `config.lock.toml` and
`.cdh-rendered`.

## Final image evidence and replay boundary

After every build mutation succeeds, cdh verifies the final image state and
exclusively writes root-owned, read-only schema-v1 evidence to
`/opt/cdh/build/manifest.json`. The manifest binds the effective-config,
canonical-lock, and BuildPlan digests and verifies the materialized cdh and
ComfyUI requirements inputs. The cdh evidence includes the canonical wheel
digest and observed installed identity. It records intended-versus-observed direct
toolchain and application identities, source and backend evidence, factual
package inventories, Manager/comfy-cli state, custom nodes, files, hooks, APT
observations, and the Tini lifecycle contract.

Immediately before publishing that evidence, the exact application interpreter
runs one mandatory isolated core probe from the installed canonical cdh wheel.
It checks torch CPU tensor execution, selected torchvision and torchaudio
imports and CPU resampling, workspace-first ComfyUI imports, and the Manager
import when enabled. This is a limited build-time smoke check, not certification
of custom nodes, workflows, GPU execution, codecs, models, service readiness,
or production health; release acceptance owns those wider runtime checks.

The manifest is observation, not resolution or replay input. It does not make
APT results, checksum-free downloads, application transitives, or trusted
installer and hook effects immutable, and it is not a claim of an offline or
byte-identical build.

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
