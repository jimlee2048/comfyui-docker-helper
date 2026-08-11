# Configuration

English | [简体中文](configuration.zh-CN.md)

This guide is for users choosing and composing the TOML input to cdh. The [strict configuration models](../../src/comfyui_docker_helper/config/final_models.py) and validation code are the machine authority; this guide explains the user-facing choices without duplicating every field.

## Choose a starting example

- [`minimal.toml`](../../examples/minimal.toml) is the smallest runnable supported configuration and the usual starting point.
- [`full.toml`](../../examples/full.toml) is a comprehensive annotated reference. Its active TOML remains valid for offline validation, but it is not an end-to-end build profile.

Every value in an example is an explicit choice made by that example, not an implied default. Start from the minimal example for a runnable configuration, and consult or copy sections from the full reference as needed.

Validate a configuration locally, without network access, Docker, or writes:

```bash
cdh host validate -f examples/minimal.toml
cdh host validate \
  -f examples/full.toml \
  --build-hooks-dir examples/build-hooks
```

Use `cdh host validate --help` for the current command options.

## Layer configuration

Repeat `-f/--file` to merge TOML files in command-line order. Tables merge recursively; a later scalar or ordinary array replaces the earlier value. The following composed collections instead merge by a field-specific identity:

- `system.extra_packages` uses the admitted Debian package name;
- `python.extra_packages`, `python.uv_tools`, and `pytorch.extra_packages` use the complete canonical requirement, including the normalized distribution name, normalized and sorted extras, and canonical selector representation;
- `comfyui.custom_nodes` uses a lowercase-only Registry resource ID or the exact direct-Git URL;
- `files` uses the `dir` plus `filename` target; and
- `cdh.git.credentials` uses the canonical credential context represented by `match`.

For package collections, a new identity appends in first-occurrence order. An exact repeated Debian package is kept once. A Python requirement is deduplicated across layers only when the complete canonical requirement is equal; cdh does not infer general range equivalence. Requirements for the same normalized distribution that differ in extras or selectors remain visible so effective validation can report the conflict. Duplicates authored in one layer likewise remain visible for validation. A later empty list resets the corresponding collection.

Registry ID case variants identify the same resource and overlay at the original position, with the later authored spelling becoming effective. Punctuation variants remain different Registry resources. If such resources map to the same normalized installed Python distribution identity, effective validation reports that collision instead of choosing one.

Canonically equivalent credential contexts identify the same route even when their raw `match` strings differ. A later route atomically replaces the complete earlier route at its original position; route fields never merge individually. Ambiguous duplicates authored in one layer remain invalid. A later `credentials = []`, `custom_nodes = []`, or `files = []` resets that collection. Each `[secrets.<name>]` table is also an atomic source definition, so a later layer can replace `env` with `file` without retaining the old field. Strict structure, uniqueness, and cross-field rules are checked after all layers have produced the effective configuration.

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

## Supply private HTTP(S) Git credentials

Define a logical Secret source under `[secrets.<name>]`, then reference that whole value from one or more `[[cdh.git.credentials]]` routes. A source selects exactly one environment variable or file; the configuration contains the locator, never the resolved value. Logical names are independent from environment-variable and file names.

```toml
[secrets.github_pat]
env = "CDH_GITHUB_PAT"

[secrets.gitlab_pat]
file = "secrets/gitlab-pat"

[[cdh.git.credentials]]
match = "https://github.com/example-private/"
username = "x-access-token"
password = { secret = "github_pat" }
```

Secret names and references must match `[a-z][a-z0-9_-]{0,63}`. An `env` locator must be a valid environment-variable name. `password` is always a structured whole-value Secret reference; it does not accept an inline token or a string interpolation.

Environment and file sources are resolved lazily within the command that needs them. Syntax-only validation never reads them, and a matching accepted lock can avoid a provider-time read. Relative file locators use the real parent of the first `-f` file as their common base; absolute and normalized parent-traversal paths are allowed. Secret files must be regular files rather than symlinks, values are limited to 65,525 bytes, and cdh warns about group or world permission bits. Git passwords must also be non-empty and contain no NUL, carriage return, or newline; create token files without a trailing newline.

Credential routes are generic HTTP(S) username/password contexts, not provider-specific objects. A GitHub or GitLab personal access token goes in the referenced `password`. The username must be non-empty; `x-access-token` is a convenient GitHub placeholder and `oauth2` is a GitLab-supported placeholder when you do not want to record a personal username. Other credential types may require a provider-prescribed username. Prefer read-only, repository-limited, expiring tokens.

`match` uses exact scheme, case-normalized host, equivalent default ports, exact non-default ports, and path-segment prefix matching. The longest matching route wins; a host-wide route ends at `/`. `http://` remains accepted but produces a warning because Basic-style credentials have no TLS transport confidentiality. A password-bearing URL userinfo is rejected; username-only userinfo must agree with the selected route username. URL rewrites, redirects, CA, and proxy behavior remain Git-owned, so a route selects credentials for the context Git presents and is not endpoint attestation.

For a build containing any direct-Git node, cdh makes every distinct Secret referenced by the effective routes available to the combined custom-node build step so recursive submodules can select their own route. Hooks, node installers, and other user-selected code in that step are trusted and can read, print, transform, or copy those credentials. cdh keeps resolved values and source locators out of its lock, BuildPlan, rendered context, manifest, image metadata, and own output; it does not sandbox trusted code or redact arbitrary output. See [Build and lock](build-and-lock.md#private-git-custom-nodes-over-https) for build and manual Buildx behavior.

When a configuration references hooks, pass the same root to validation, rendering, or building:

```bash
cdh host validate \
  -f cdh.toml \
  --build-hooks-dir build-hooks
```

## Next steps

- [Build and lock](build-and-lock.md) explains how the effective configuration is validated, resolved, rendered, and built.
- [Runtime](runtime.md) explains which baked settings can be overridden when a container starts.
