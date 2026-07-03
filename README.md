# comfyui-docker-helper

Build customized ComfyUI Docker images from a declarative TOML configuration.

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
  custom nodes, and configured files.

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
- `scripts/` only when hook scripts are referenced.

The context is retained so you can inspect the Dockerfile, rendered
configuration, lock file, and scripts after failures.

## Custom-node hooks and scripts

Hook scripts referenced by custom nodes must be relative paths ending in `.sh`
or `.py`. If any hook is referenced, pass the scripts directory:

```bash
cdh host build \
  -f config.toml \
  -t comfyui-custom:dev \
  --scripts-dir ./scripts
```

When hooks are present, the whole scripts directory is copied into the build
context and bind-mounted during the helper step. Keep unrelated sensitive files
out of that directory.

## Files and downloaders

Configured files are downloaded during the Docker build by
`cdh container download-files`. The available download backends are:

- `httpx` for simple HTTP(S) downloads with retries and temporary-file rename;
- `aria2` for build-time RPC-controlled downloads with per-file serialization.

Files are processed in configuration order, one active item at a time. Existing
targets are skipped unless `overwrite = true`. Each file entry must declare an
explicit `filename`; download targets are not inferred from URLs. Set the
default downloader under `[cdh]`, and override it per file with `downloader`.
Configure package indexes with `python.index_url` and
`pytorch.index_base_url`.

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
| custom node git install with hooks | hook copy/mount and subprocess behavior |
| httpx file download | local/remote file download path |
| aria2 file download | real aria2 daemon path |
| full config | combined nodes, hooks, files, env, CMD, and launch args |

These commands are resource-heavy. Run them deliberately after checking that
network, disk, Docker cache, and CUDA base image requirements are acceptable for
your machine. The lightweight fixture validation lives in `tests/smoke/` and
does not run Docker.

Record which checks used real upstream services and which used local fixtures.

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
