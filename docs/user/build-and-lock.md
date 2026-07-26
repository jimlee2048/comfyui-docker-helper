# Build and lock images

English | [简体中文](build-and-lock.zh-CN.md)

This guide covers local validation, canonical-lock reconciliation, rendered
build contexts, and Docker image builds. Start with the
[configuration guide](configuration.md) to choose and layer configuration
files. Commands below are run from the repository root.

## Validate, render, and build

Validate configuration before resolving or building anything:

```bash
cdh host validate -f examples/minimal.toml
```

Validation is local: it makes no provider or Docker calls and writes no files.
Repeat `-f/--file` to use configuration layers; cdh merges them in command-line
order and validates the effective result.

Render a reusable build context and canonical lock:

```bash
cdh host render \
  -f examples/minimal.toml \
  -o .cdh/build/current \
  --overwrite
```

Rendering reuses a matching lock. It may use Docker when a missing or changed
uv-backed result must be resolved. `--overwrite` replaces only an existing
valid cdh-owned context; otherwise cdh refuses the replacement.

Build an image with Docker Buildx:

```bash
cdh host build \
  -f examples/minimal.toml \
  --context-dir .cdh/build/current \
  -t my-comfy:dev \
  --load
```

`host build` renders the selected context and then invokes Buildx. Supply one
or more `-t/--tag` values or configure `[build].tags`. Use `--load` for the
local Docker image store or `--push` for a registry; the two options are
mutually exclusive.

## Hook source directories

Build hooks referenced by custom-node configuration have no implicit source
directory. Pass `--build-hooks-dir` to `validate`, `render`, and `build`:

```bash
cdh host validate \
  -f examples/full.toml \
  --build-hooks-dir examples/build-hooks
```

Paths in configuration are relative to this directory. cdh admits only the
referenced regular `.sh` and `.py` files and preserves their safe relative
layout. Build hooks are trusted code, and their verified source bytes remain
in the final image and its layers. Do not put secrets in them. See the
[build-hook examples](../../examples/build-hooks/).

Pass `--runtime-hooks-dir` to `render` or `build` to bake a runtime hook tree:

```bash
cdh host render \
  -f examples/full.toml \
  -o .cdh/build/full \
  --build-hooks-dir examples/build-hooks \
  --runtime-hooks-dir examples/runtime-hooks \
  --overwrite
```

The runtime hook directory may contain only supported hook files directly
under `pre-start.d/`, `post-start.d/`, and `stop.d/`. Omitting the option bakes
no runtime hook tree. Mounted runtime hooks are separate deployment-time
inputs; see the [runtime guide](runtime.md) and
[runtime-hook examples](../../examples/runtime-hooks/).

## From effective configuration to a context

cdh uses one forward-only planning flow:

```text
effective configuration -> canonical lock -> BuildPlan -> rendered context
```

The effective configuration describes intent. `config.lock.toml` records the
accepted exact external and local content identities used for host
reconciliation. cdh then constructs one immutable BuildPlan, which is the
build-time execution authority. Context rendering projects that plan together
with its exact wheel and verified hook inputs; build-time helpers do not
re-read host configuration or the lock to make new planning decisions.

## Reconciliation modes

Provider policy and filesystem/build side effects are separate. Choose among
these five user-facing modes:

| Mode | Resolution behavior | Context and build behavior |
| --- | --- | --- |
| Default | Reuse unchanged entries, resolve missing or changed inputs, and remove deleted identities. | Atomically write the accepted lock and rendered context. |
| `--locked` | Require the existing lock and local inputs to match exactly; make no provider or Docker calls during reconciliation. | Compare the existing context and write nothing. `host build` still invokes Buildx after the checks pass. |
| `--upgrade-lock` | Refresh moving selectors while retaining unchanged exact selections. | Atomically write the updated lock and context. |
| `--check` | Apply default reconciliation policy. | Compare the complete expected context with the existing one; write nothing and do not build. |
| `--dry-run` | Use default policy unless combined with `--locked` or `--upgrade-lock`. | Print the exact BuildPlan preview; write nothing and do not build. |

`--check` cannot be combined with a lock-policy or dry-run modifier.
`--locked` and `--upgrade-lock` are mutually exclusive.
When `--dry-run` is combined with a lock policy, preview behavior replaces
context comparison or publication.

No-write does not necessarily mean offline. Default, `--check`, and
`--dry-run` may call providers and may require Docker when the current lock
cannot supply a required uv-backed result. A complete matching lock keeps
those paths Docker-free. Only `--locked` forbids provider and Docker calls
during reconciliation; Docker Buildx remains a separate requirement for
`host build`.

Malformed or unsupported lock files fail closed with a diagnostic instructing
you to remove and regenerate the lock.

## Rendered context

A rendered context contains:

- `.cdh-rendered`, the host marker for a cdh-owned context;
- `config.lock.toml`, host-only reconciliation state;
- `build-plan.json`, the canonical build-time execution plan;
- `bootstrap/comfyui_docker_helper-<version>-py3-none-any.whl`, the exact
  validated cdh wheel installed into the image;
- `build/hooks/`, containing only referenced verified build-hook bytes when
  configured;
- `runtime/config.toml`, derived from the BuildPlan;
- `runtime/hooks/`, containing the verified baked runtime hook tree when
  configured;
- `Dockerfile`, rendered with literal digest-qualified base-image references;
  and
- `.dockerignore`, which excludes `config.lock.toml` and `.cdh-rendered` from
  Buildx input.

The context contains no root `config.toml`. Host-local source paths are not
BuildPlan inputs, and the Dockerfile has no argument that can replace
lock-authoritative image identities.

## Python environments and package sources

The image keeps application packages and user tools in separate ownership
domains:

- `/opt/venv` contains ComfyUI, its application dependencies, and the optional
  checkout-owned Manager/`cm-cli` capability.
- cdh, optional `comfy-cli`, and each configured `[python].uv_tools` package
  use separate environments under `/opt/uv/tools`.
- tool commands are linked under `/opt/uv/bin`; executable ownership
  collisions fail instead of replacing an existing command.

`comfy-cli` is an optional user tool and is not used to install ComfyUI,
Manager, or Registry custom nodes during the image build.

cdh-controlled ordinary Python resolution and installation use
`[python].index_url`. Direct PyTorch packages and target-active protected
requirements from the selected ComfyUI checkout form one exact group and use
only the CUDA-derived PyTorch source. Their generic transitive dependencies use
the ordinary Python source. A missing direct PyTorch member does not fall back
to a same-named package on the ordinary source, and the selected exact group is
protected from later cdh-controlled application mutations.

## Final evidence and replay boundary

After all image mutations succeed, cdh writes the strict final-state
observation `/opt/cdh/build/manifest.json`. It binds the effective
configuration, canonical lock, and BuildPlan digests and records intended and
observed direct identities. The manifest is evidence, not another resolver,
lock, replay input, support verdict, or general service-health check.

cdh provides bounded verified replay of cdh-controlled direct inputs. This
does not promise an offline or byte-identical build, a complete lock of
transitive dependencies or every fetched artifact, authenticity for downloads
without a user-supplied checksum, deterministic effects from trusted
installers or hooks, or replay of deployment-time mutations.
