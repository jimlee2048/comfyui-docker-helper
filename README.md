# comfyui-docker-helper

Build customized ComfyUI Docker images from a declarative TOML configuration.
Rendered images start through the `cdh` entrypoint so runtime defaults,
mounted overrides, downloads, lifecycle hooks, and ComfyUI startup arguments are
handled consistently.

This project targets advanced local users who are already comfortable with
Docker Buildx, ComfyUI, Python packaging, and build-time Docker diagnostics.
Rendered contexts contain root `config.toml` and `config.lock.toml`, and host
builds can load or push one or more image tags from either CLI flags or
configuration.

## Prerequisites

- Python 3.12 or newer.
- `uv` for local execution and package management.
- Docker with Buildx.
- BuildKit support for Dockerfile `RUN --mount=type=bind`.
- Network access during Docker builds for base images, Python packages, ComfyUI,
  custom nodes, and configured files. Container startup may also need network
  access when effective runtime file downloads target missing files or need to
  refresh changed `overwrite = true` sources.

The rendered image installs `aria2` inside the build container. A host-side
`aria2c` binary is not required unless you are running the optional local smoke
tests that explicitly use it.

## Install for local use

After a release is available on PyPI, install the CLI with one of:

```bash
uv tool install comfyui-docker-helper
pipx install comfyui-docker-helper
python -m pip install comfyui-docker-helper
```

From a checkout:

```bash
uv sync --locked
uv run cdh --help
```

To install the CLI from the current directory into a uv-managed tool
environment:

```bash
uv tool install .
cdh --help
```

## Example configurations

User-facing starting points live in the repository's `examples/` directory:

- [`examples/minimal.toml`](https://github.com/jimlee2048/comfyui-docker-helper/blob/main/examples/minimal.toml)
  is a small copyable config.
- [`examples/full.toml`](https://github.com/jimlee2048/comfyui-docker-helper/blob/main/examples/full.toml)
  is an annotated reference for host-build and common configuration fields.
- [`examples/README.md`](https://github.com/jimlee2048/comfyui-docker-helper/blob/main/examples/README.md)
  shows validate, render, and build commands.

These files are linked to the source repository so they are useful from PyPI
and do not require the examples directory to be present inside an installed
wheel.

## Commands

Validate without writing a build context:

```bash
cdh host validate -f config.toml
```

Repeat `-f/--file` to layer partial TOML overrides; later files take priority.
The examples use a single root `config.toml` shape with supported blocks such
as `[cdh]`, `[build]`, `[python]`, `[pytorch]`, `[comfyui]`, and `[[files]]`.

Render an inspectable Docker build context:

```bash
cdh host render -f config.toml -o .cdh/build/current --overwrite
```

Use `--locked` to render from an existing context `config.lock.toml`, or
`--upgrade-lock` to refresh moving selectors before writing a new lock file.
Lock artifacts live in the rendered context root; there is no separate
source-side lock command.

Build and load a local Docker image:

```bash
cdh host build -f config.toml -t comfyui-custom:dev --load
```

Repeat `-t/--tag` to build multiple effective image tags. CLI tags replace
`[build].tags`; if no CLI tags are passed, `cdh host build` uses `[build].tags`
from the configuration. Use `--load` or `--push` to override `[build].output`
for the current build. Use `--context-dir <dir>` to choose a non-default
rendered context location.

`cdh host build` is equivalent to rendering the context with safe internal
overwrite semantics and then running:

```bash
docker buildx build --load -t comfyui-custom:dev .cdh/build/current
```

The command preserves the rendered context after both success and failure. It
does not expose `--clean-context`, does not use the Docker SDK, and does not
fall back to `docker build`.

## Context and marker safety

The default build context is `.cdh/build/current`. Rendered contexts contain a
`.cdh-rendered` marker. Automatic replacement is only allowed for directories
with a valid marker; unmarked directories are refused even when a command uses
overwrite behavior internally.

Rendered contexts are intentionally inspectable. They include:

- `Dockerfile`;
- a minimal projected `packages/cdh` Python package used by build-time helpers;
- `config.toml`, the validated configuration used for the render;
- `config.lock.toml`, the resolved source and lock selections used by
  container helpers;
- `runtime/config.toml`, the runtime-supported defaults baked into the image;
- `runtime/hooks/` only when runtime lifecycle hooks are supplied;
- `scripts/` only when custom-node hook scripts are referenced.

The context is retained so you can inspect the Dockerfile, rendered
configuration, lock file, and scripts after failures.

## Custom-node Hooks and Scripts

Custom-node hook scripts run during image build around custom-node
installation. Hook paths referenced by custom nodes must be relative paths
ending in `.sh` or `.py`. If any custom-node hook is referenced, pass the
scripts directory:

```bash
cdh host build \
  -f config.toml \
  -t comfyui-custom:dev \
  --scripts-dir ./scripts
```

When custom-node hooks are present, the whole scripts directory is copied into
the build context and bind-mounted during the helper step. Keep unrelated
sensitive files out of that directory.

## Runtime Startup and Overrides

Rendered images use:

```dockerfile
ENTRYPOINT ["cdh", "container", "entrypoint"]
```

At container startup, the entrypoint loads runtime defaults in this order:

1. built-in defaults;
2. baked `/opt/cdh/runtime/config.toml`;
3. optional mounted `/etc/cdh/runtime/config.toml`;
4. supported environment overrides.

The baked runtime config contains only runtime-supported fields: `[comfyui]`
startup values (`listen`, `port`, and `extra_args`), `[cdh]` downloader
defaults/backend settings, retry and failure-policy settings, `[system.ssh]`,
and any `[[files]]` defaults. Host-only build fields such as base image,
Python, PyTorch, image tags, ComfyUI version selection, and custom-node sources
are not written into the runtime config.

The entrypoint starts ComfyUI with the effective runtime values:

```bash
python "$COMFYUI_PATH/main.py" --listen "$LISTEN" --port "$PORT" --disable-auto-launch "${EXTRA_ARGS[@]}"
```

Supported startup environment overrides are `CDH_COMFYUI_LISTEN`,
`CDH_COMFYUI_PORT`, `CDH_COMFYUI_EXTRA_ARGS`, `CDH_DEFAULT_DOWNLOADER`,
`CDH_DEFAULT_DOWNLOAD_MODE`, `CDH_DOWNLOAD_MAX_ATTEMPTS`,
`CDH_DOWNLOAD_FAILURE_POLICY`, `SSH_ENABLE`, `SSH_PORT`, `SSH_PASSWORD`, and
`SSH_PUB_KEY`. Environment overrides replace the corresponding runtime
defaults; downloader and mode overrides do not rewrite explicit per-file
values.

## Files and downloaders

Host `[[files]]` entries are downloaded into the image during Docker build and
are also baked as runtime defaults. Host build downloads always run
synchronously, even when runtime configuration sets `download_mode = "async"`.
At container startup, effective runtime files use their configured mode:
`sync` files run before pre-start hooks and before ComfyUI is spawned, while
`async` files are accepted into a cdh-managed background queue and do not block
ComfyUI startup, readiness checks, or post-start hooks after the queue has
started.

The available download backends are:

- `httpx` for simple HTTP(S) downloads with retries and temporary-file rename;
- `aria2` for RPC-controlled downloads with per-file serialization.

Files are processed in configuration order, one active item at a time. Existing
targets are skipped unless `overwrite = true`. With `overwrite = true`, cdh
does not redownload on every start once runtime state records the current
source as completed and the final file still exists. Each file entry must
declare an explicit `filename`; download targets are not inferred from URLs.
Set the default downloader under `[cdh]`, and override it per file with
`downloader`. Set `default_download_mode = "sync"` or `"async"` under `[cdh]`,
and override it per file with `download_mode`.

Runtime downloads use `download_max_attempts` as a per-file, per-container-start
attempt budget. `download_failure_policy = "continue"` records exhausted
failures and continues with later files; `download_failure_policy = "fail"`
fails startup for exhausted sync downloads, and stops scheduling later async
files for the current start without terminating an already-running ComfyUI
process. Attempt budgets reset on the next container start.

Runtime download state is stored at `/var/lib/cdh/runtime/state.json`. Use a
persistent volume for `/var/lib/cdh/runtime` when you want restart
reconciliation, completed-source tracking, and retry state to survive container
replacement. Missing state is treated as a first run. Corrupt or unsupported
state prevents startup only when runtime downloads are configured.

Incomplete runtime downloads are kept out of final target paths. cdh stages
downloads beside the target under a target-local `.cdh-staging/` directory,
using cdh-owned filenames, and only removes cdh-owned stale staging files after
the safety window. It does not delete unrecognized staging files or files
outside cdh-owned staging directories.

Mounted runtime configs merge file entries by normalized target path, with
later same-target entries taking priority; `files = []` clears earlier runtime
file defaults. Configure package indexes with `python.index_url` and
`pytorch.index_base_url`.

## SSH runtime access

Rendered images include OpenSSH server capability, but SSH is disabled by
default and cdh starts `sshd` only when effective runtime config enables it and
at least one valid credential exists. Configure baked defaults with
`[system.ssh]`, override them with `/etc/cdh/runtime/config.toml`, or use
runtime environment variables:

```text
SSH_ENABLE=true
SSH_PORT=22
SSH_PASSWORD=...
SSH_PUB_KEY="ssh-ed25519 ..."
```

SSH login is for `root`. Password and public-key authentication can both be
enabled when both credentials are present. Root SSH access is powerful; protect
runtime configs, rendered contexts, images, registries, environment variables,
and logs accordingly. Prefer runtime environment variables or mounted runtime
config for real credentials, not baked image config.

cdh controls only the container-internal `sshd` port. Docker host port
publication and network exposure are deployment responsibilities, for example
`docker run -p 2222:22 ...`. Dockerfile `EXPOSE` metadata, when present, does
not publish a host port. ComfyUI authentication and any reverse-proxy or Docker
network access controls are also outside cdh's scope.

## Runtime Lifecycle Hooks

Runtime lifecycle hooks are separate from custom-node build hooks. Pass
`--hooks-dir <dir>` to `cdh host render` or `cdh host build` to bake a runtime
hook tree into `/opt/cdh/runtime/hooks`; mount another tree at
`/etc/cdh/runtime/hooks` to add runtime hook files.

A runtime hook tree can contain these phase directories:

```text
pre-start.d/
post-start.d/
stop.d/
```

Hook files must be regular `.sh` or `.py` files. Baked hooks are discovered
before mounted hooks, and hook files run in lexical order within each phase.
Pre-start hooks run after runtime downloads and before ComfyUI starts. When
post-start hooks exist, the entrypoint waits for ComfyUI readiness at
`http://127.0.0.1:<port>/system_stats` before running them. After normal
startup has completed, a graceful `SIGTERM` or `SIGINT` runs stop hooks before
forwarding the original signal to ComfyUI; stop-hook failures are logged without
overriding ComfyUI's final result.

## Secrets and logs

Values in `[system.env]` are ordinary Dockerfile `ENV` values. Token-like
values are not hidden, prevented from appearing in image history, or redacted
from build output. Do not put secrets in configuration unless that exposure is
acceptable.

The temporary aria2 RPC secret is generated inside the helper process and is not
written to helper configuration or normal helper logs.

## ComfyUI build smoke fixtures

Long-term ComfyUI build smoke inputs live under
`tests/fixtures/comfyui-build/`. The fixture README lists the exact commands
for real Docker builds:

| Scenario | Purpose |
| --- | --- |
| minimal config | base render/build/load path |
| custom node registry install | Manager registry path and cache update |
| custom node git install with hooks | hook copy/mount and hook runner behavior |
| httpx file download | local/remote file download path |
| aria2 file download | real aria2 daemon path |
| full config | combined nodes, hooks, files, env, entrypoint, and startup args |

These commands are opt-in and resource-heavy. Run them deliberately after
checking that network, disk, Docker cache, and CUDA base image requirements are
acceptable for your machine. The lightweight fixture validation lives in
`tests/smoke/` and does not run Docker.

When recording smoke results, note which checks used real upstream services and
which used local fixtures so failures can be classified consistently.

## Development

Install the locked development environment and run quality gates with:

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Apply automatic formatting with:

```bash
uv run ruff format .
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
