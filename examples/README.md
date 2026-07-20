# Configuration examples

`minimal.toml` is the smallest canonical CUDA 13 / PyTorch 2.12 configuration.
`full.toml` documents every field in the current public configuration schema.
Both select ComfyUI v0.11.0, the minimum supported release; any resolved
formal, moving, nightly, or full-commit checkout must descend from that floor.

Validate locally without providers or writes:

```bash
cdh host validate -f examples/minimal.toml
cdh host validate -f examples/full.toml --scripts-dir examples/scripts
```

Render with canonical reconciliation:

```bash
cdh host render \
  -f examples/minimal.toml \
  -o .cdh/build/minimal \
  --hooks-dir examples/hooks \
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

The optional `--hooks-dir` tree may contain only regular `.sh` or
`.py` files directly under `pre-start.d/`, `post-start.d/`, and `stop.d/`.
Every baked hook is content-locked and copied to `/opt/cdh/runtime/hooks`;
mounted `/etc/cdh/runtime/hooks` remains external runtime input. Omitting
`--hooks-dir` means that no runtime hook tree is baked.

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
`[comfyui].install_manager` separately controls the exact checkout-declared
Manager package and `/opt/venv/bin/cm-cli` application capability. Enabling the
capability does not append `--enable-manager`; runtime arguments remain explicit
under `comfyui.extra_args`. Registry and direct-Git nodes run once in their
declared mixed order, remain independent of optional comfy-cli, and produce
verified ordered evidence in `/opt/cdh/build/manifest.json` after the final
filesystem is observed.
Direct-Git URLs pass through unchanged as acquisition locators; locked exact
commits, not user refs, URL normalization, or endpoint claims, identify the
content installed in the image. Manager/Registry installers and direct-Git
`install.py` remain trusted opaque code rather than network-isolated package
operations.
Zero-node builds still record exact empty custom-node evidence and the factual
final application inventory after the dependency check. Custom-node build-hook
source is retained under `/opt/cdh/build/inputs` and in image layers as verified
audit evidence; do not put secrets in these scripts.

The image contains a baked runtime configuration at
`/opt/cdh/runtime/config.toml`. A mounted `/etc/cdh/runtime/config.toml` can
override runtime-only ComfyUI, downloader, SSH, and file settings without an
image rebuild. See the root README for the complete runtime precedence,
environment-variable, download-state, and lifecycle contracts.
