# Configuration examples

`minimal.toml` is the smallest canonical CUDA 13 / PyTorch 2.12 configuration.
`full.toml` documents every field in the current public configuration schema.

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

The output contains canonical `config.lock.toml`, `build-plan.json`,
`manifest-binding.json`, narrow `phases/*.json`, a BuildPlan-derived runtime
config, verified referenced build-hook bytes, content-locked baked runtime
hooks, and a Dockerfile with literal digest-qualified `FROM` references. It
does not copy root config into the container-helper authority.

The optional `--hooks-dir` tree may contain only regular `.sh` or
`.py` files directly under `pre-start.d/`, `post-start.d/`, and `stop.d/`.
Every baked hook is content-locked and copied to `/opt/cdh/runtime/hooks`;
mounted `/etc/cdh/runtime/hooks` remains external runtime input.

Use `--dry-run` for an exact no-write preview, `--check` to compare an existing
context, `--locked` for zero-provider/zero-write verification, and
`--upgrade-lock` to refresh moving selectors.

The public PyTorch version is a selector. Its CUDA-derived channel, index, and
target enter the resolver request identity, while the canonical lock and
BuildPlan retain complete resolved versions such as `2.12.1+cu130`.
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
verified ordered evidence at `/opt/cdh/build/custom-node-inventory.json`.
Direct-Git URLs pass through unchanged as acquisition locators; locked exact
commits, not URL normalization or endpoint claims, identify the content.
Zero-node builds still emit the exact empty custom-node inventory and the final
application inventory after the dependency check.

The image contains a baked runtime configuration at
`/opt/cdh/runtime/config.toml`. A mounted `/etc/cdh/runtime/config.toml` can
override runtime-only ComfyUI, downloader, SSH, and file settings without an
image rebuild. See the root README for the complete runtime precedence,
environment-variable, download-state, and lifecycle contracts.
