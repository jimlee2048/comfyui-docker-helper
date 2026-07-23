# Configuration guide

cdh's strict Pydantic models are the machine authority for the public
configuration schema. This guide and the annotated examples explain how to use
that schema:

- [`minimal.toml`](minimal.toml) is the smallest runnable supported
  configuration example.
- [`full.toml`](full.toml) is the complete annotated field reference.

Values selected by these examples are explicit supported choices, not implied
defaults. `full.toml` labels fields as required or identifies their actual
defaults. Both examples select ComfyUI v0.11.0, the minimum supported release;
any resolved formal, moving, nightly, or full-commit checkout must descend from
that floor.

## Quick start

Validate locally without providers or writes:

```bash
cdh host validate -f examples/minimal.toml
cdh host validate -f examples/full.toml --build-hooks-dir examples/build-hooks
```

Render with canonical reconciliation:

```bash
cdh host render \
  -f examples/minimal.toml \
  -o .cdh/build/minimal \
  --runtime-hooks-dir examples/runtime-hooks \
  --overwrite
```

Key output includes canonical `config.lock.toml`, one canonical
`build-plan.json`, a BuildPlan-derived runtime config, one canonical cdh wheel,
verified referenced build-hook bytes, content-locked baked runtime hooks, and a
Dockerfile with
literal digest-qualified `FROM` references. It does not copy root config into
the container-helper authority. After the image's final build mutation, cdh
runs one limited core application smoke through the exact isolated application
interpreter, then writes `/opt/cdh/build/manifest.json` as digest-bound
observational evidence. The smoke does not certify arbitrary custom nodes,
workflows, GPU behavior, codecs, models, or service health, and the in-image
manifest is not another resolver or replay input.

The optional `--runtime-hooks-dir` source may contain only regular `.sh` or
`.py` files directly under `pre-start.d/`, `post-start.d/`, and `stop.d/`;
unknown top-level entries are rejected before rendering. Every baked hook is
content-locked and copied to `/opt/cdh/runtime/hooks`. Mounted
`/etc/cdh/runtime/hooks` remains external runtime input: unrelated top-level
entries are ignored, but contents of recognized phase directories are strictly
validated. Omitting `--runtime-hooks-dir` means that no runtime hook tree is
baked.

`--build-hooks-dir` has no default and is required only when configuration
references `pre_install_hooks` or `post_install_hooks`. Paths are resolved
relative to that explicit root. cdh preserves the user's safe relative layout,
admits only referenced files, and does not require owner or phase subfolders.

If a startup hook launches a background service, define a matching stop hook
that requests the service's termination and waits for it to exit. Prefer the
service's own control command or API. A PID file is also possible, but the hook
author must validate stale files, PID reuse, and process identity. cdh manages
the hook only while its leader is active; after a successful return, the
background service is user-managed. Tini runs as image PID 1 to forward Docker
signals to cdh and reap adopted zombies, but it is not a service supervisor.
There is no graceful-stop guarantee when the stop hook is absent or fails,
ComfyUI exits naturally, Docker sends `SIGKILL`, or PID 1 exits early.

Use `--dry-run` for an exact no-write preview, `--check` to compare an existing
context, `--locked` for zero-provider/zero-write verification, and
`--upgrade-lock` to refresh moving selectors.
A complete matching lock keeps default, check, dry-run, and locked
reconciliation Docker-free. Missing or changed uv-backed canonical results
require Docker-backed resolution; `host build` additionally requires Docker
Buildx.

`[python].uv_version` defaults to `latest` and also accepts an exact stable
`X.Y.Z`. cdh derives the official Debian-slim uv image tag, then locks its exact
digest and observed uv version; it does not execute a host uv. Configure an
exact release when the request itself must stay fixed before lock resolution.
Ordinary target-Python package resolution uses the explicitly configured
`[python].index_url` rather than ambient host pip/uv configuration. That setting
does not configure canonical-wheel backend provisioning: PyPA isolated backend
acquisition follows the host Python packaging environment. See the
[root README](../README.md) for Docker connection, proxy, and private-CA
boundaries.

Direct declarations in `[python].extra_packages`, `[python].uv_tools`, and
`[pytorch].extra_packages` accept a bare package name, an exact stable version,
or a bounded comparison selector. URL, VCS, local, editable, raw-option, and
environment-marker forms are rejected.

The public PyTorch version is a selector. Its CUDA-derived channel, index, and
target enter the resolver request identity, while the canonical lock and
BuildPlan retain complete resolved versions such as `2.12.1+cu130`.
Configured torch and PyTorch extras are merged with target-active protected
requirements from the exact ComfyUI checkout. Every direct member uses the
derived PyTorch source; generic transitives and all ordinary cdh-controlled
Python installation use `[python].index_url`.
CUDA `image_flavor` and `image_distro` independently select the exact NVIDIA
image tag; they do not alter the channel derived from the CUDA version. An
upstream tag or target-platform miss fails resolution without substitution.
Successfully resolving a `base` or `runtime` image does not promise development
headers or cuDNN capabilities. Dependent install, build, or runtime gates may
still fail without fallback.

`full.toml` also demonstrates `[python].uv_tools`. Each entry is resolved and
locked as an isolated direct package, then installed under `/opt/uv/tools` with
executables linked under `/opt/uv/bin`. The application interpreter and its
`pip` commands remain under `/opt/venv`; cdh is the first isolated tool and
executable collisions are fatal. `[comfyui].install_cli` independently controls
the isolated user-facing comfy-cli tool; enabled mode installs its exact locked
release before generic tools and disabled mode omits it completely.
The `comfy-cli` distribution remains reserved to `[comfyui].install_cli` in
both modes and must not be declared in Python extras, PyTorch extras, or uv
tools.

`[comfyui].install_manager` separately controls the exact checkout-declared
Manager package and `/opt/venv/bin/cm-cli` application capability. Enabling the
capability does not append `--enable-manager`; runtime arguments remain explicit
under `comfyui.extra_args`. Registry and direct-Git nodes run once in their
declared mixed order, remain independent of optional comfy-cli, and produce
verified ordered evidence in `/opt/cdh/build/manifest.json` after the final
filesystem is observed.
Registry custom-node IDs must be valid Python project names and remain unique
after normalized project-name comparison.

Direct-Git URLs pass through unchanged as acquisition locators; locked exact
commits, not user refs, URL normalization, or endpoint claims, identify the
content installed in the image. Manager/Registry installers and direct-Git
`install.py` remain trusted opaque code rather than network-isolated package
operations.
Zero-node builds still record exact empty custom-node evidence and the factual
final application inventory after the dependency check. Referenced build-hook
source is retained under `/opt/cdh/build/hooks` and in image layers as verified
audit evidence; do not put secrets in these hook files.

The image contains a baked runtime configuration at
`/opt/cdh/runtime/config.toml`. A mounted `/etc/cdh/runtime/config.toml` can
override runtime-only ComfyUI, downloader, SSH, and file settings without an
image rebuild. See the root README for the complete runtime precedence,
environment-variable, download-state, and lifecycle contracts.
