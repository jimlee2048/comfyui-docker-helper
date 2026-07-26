# Configuration

English | [简体中文](configuration.zh-CN.md)

This guide is for users choosing and composing the TOML input to cdh. The [strict configuration models](../../src/comfyui_docker_helper/config/final_models.py) and validation code are the machine authority; this guide explains the user-facing choices without duplicating every field.

## Choose a starting example

- [`minimal.toml`](../../examples/minimal.toml) is the smallest runnable supported configuration.
- [`full.toml`](../../examples/full.toml) is the comprehensive annotated example. It marks required fields and documents actual defaults.

Every value in an example is an explicit choice made by that example, not an implied default. Copy the closest example and remove or change selections for your image.

Validate a configuration locally, without network access, Docker, or writes:

```bash
cdh host validate -f examples/minimal.toml
cdh host validate \
  -f examples/full.toml \
  --build-hooks-dir examples/build-hooks
```

Use `cdh host validate --help` for the current command options.

## Layer configuration

Repeat `-f/--file` to merge TOML files in command-line order. Tables merge recursively; a later scalar or ordinary array replaces the earlier value. Two collections merge by stable identity:

- `comfyui.custom_nodes` uses the Registry ID or direct-Git URL; and
- `files` uses `dir` plus `filename`.

A later empty `custom_nodes = []` or `files = []` resets that collection. Strict structure, uniqueness, and cross-field rules are checked after all layers have produced the effective configuration.

For example, save this as `local.toml` to disable comfy-cli and remove the nodes and files selected by the full example:

```toml
files = []

[comfyui]
install_cli = false
custom_nodes = []
```

Then validate the effective configuration:

```bash
cdh host validate -f examples/full.toml -f local.toml
```

## Supported selections

The package supports Python 3.12 through 3.14. `python.version` must select one exact stable CPython patch in that range; when omitted, it defaults to `3.13.14`. cdh does not silently switch to another Python version. The package support range is defined by [`pyproject.toml`](../../pyproject.toml).

The current compute backend is CUDA and the current image target is `linux/amd64`. CUDA and PyTorch versions are required explicit selections. ComfyUI also requires an explicit selector: `latest`, `nightly`, a full lowercase commit, an exact semantic release with or without a `v` prefix, or a supported version constraint. cdh resolves that selector only from the official ComfyUI repository, and every accepted checkout must descend from the supported v0.11.0 floor.

The full example documents the accepted selector forms and the remaining defaults. Unsupported upstream image tags, versions, or target combinations fail instead of being silently substituted.

## Choose application and tool capabilities

Manager and comfy-cli are independently controlled optional capabilities. Both are enabled when their switches are omitted, and either can be disabled separately:

- `comfyui.install_manager` controls the Manager capability from the selected ComfyUI checkout and its `cm-cli` Registry interface. Registry custom nodes require Manager. Enabling it does not add `--enable-manager` to ComfyUI runtime arguments.
- `comfyui.install_cli` controls the separately resolved user-facing comfy-cli tool. cdh does not use comfy-cli to build the image or install Registry nodes.

Entries in `python.uv_tools` request additional isolated command-line tools. They do not install packages into the ComfyUI application environment. See the [build and lock guide](build-and-lock.md) for package-source, resolution, and tool-environment behavior.

## Choose custom nodes and build hooks

Custom nodes may use either a Registry identity or a direct-Git URL. Registry nodes require Manager; direct-Git nodes do not. Mixed declarations retain their effective configuration order. Set `custom_nodes = []` in a later layer to remove inherited nodes.

Each node may name pre-install or post-install hooks. Hook paths are relative to the directory passed explicitly with `--build-hooks-dir`; there is no implicit hook root. The repository includes small [`pre.sh`](../../examples/build-hooks/pre.sh) and [`post.sh`](../../examples/build-hooks/post.sh) examples.

Build hooks and custom-node installers execute trusted user-selected code during the image build. Review them before use, and do not put secrets in hook files because their contents remain in the image and its layers.

When a configuration references hooks, pass the same root to validation, rendering, or building:

```bash
cdh host validate \
  -f examples/full.toml \
  --build-hooks-dir examples/build-hooks
```

## Next steps

- [Build and lock](build-and-lock.md) explains how the effective configuration is validated, resolved, rendered, and built.
- [Runtime](runtime.md) explains which baked settings can be overridden when a container starts.
