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
- `python.extra_packages`, `python.uv_tools`, and `pytorch.extra_packages` use the complete canonical requirement, including the normalized distribution name, normalized and sorted extras, selector or named direct reference, and marker;
- `comfyui.custom_nodes` uses a lowercase-only Registry resource ID or the exact direct-Git URL;
- `files` uses the normalized `target_dir` plus `filename` target; and
- `cdh.downloader.credentials` uses the canonical HTTP(S) origin and path represented by `match`; and
- `cdh.git.credentials` uses the canonical credential context represented by `match`.

For package collections, a new identity appends in first-occurrence order. An exact Debian package repeated across uniquely keyed layers is kept once. If an effective `system.extra_packages` item is already in cdh's default OS package set, cdh warns at its source and omits the redundant item from the effective installation request. Duplicates authored together in one user-owned list remain errors. A Python requirement is deduplicated across layers only when the complete canonical requirement is equal; cdh does not infer general range equivalence. Requirements for the same normalized distribution that differ in extras, selector, direct source, or marker remain available until markers are evaluated for the selected target. Multiple active declarations for that distribution then conflict within the application packages or isolated tools. Duplicates authored in one layer likewise remain visible for validation. A later empty list resets the corresponding collection.

`system.ssh.pub_keys` remains an ordinary whole-list replacement across TOML layers: omission inherits, a later non-empty list replaces, and `[]` clears it. After the winning list is selected, cdh trims each line, drops empty values, and stably deduplicates by declared key type plus base64 key blob. It retains the first normalized complete line, including its optional comment. Each later non-empty duplicate produces a source-aware warning that does not print key material.

Copy public-key values from the ordinary public-key line in `.pub` output produced by standard OpenSSH tools. Do not include an `authorized_keys` options prefix. Authenticator-hosted Ed25519 and ECDSA P-256 keys (`sk-ssh-ed25519@openssh.com` and `sk-ecdsa-sha2-nistp256@openssh.com`) are supported. OpenSSH certificates and options such as `restrict`, `from=`, and `command=` are not accepted configuration syntax.

Registry ID case variants identify the same resource and overlay at the original position, with the later authored spelling becoming effective. Punctuation variants remain different Registry resources. If such resources map to the same normalized installed Python distribution identity, effective validation reports that collision instead of choosing one.

Canonically equivalent credential contexts identify the same route even when their raw `match` strings differ. A later route atomically replaces the complete earlier route at its original position; route fields never merge individually. Ambiguous duplicates authored in one layer remain invalid. A later `credentials = []`, `custom_nodes = []`, or `files = []` resets that collection. Each `[secrets.<name>]` table is also an atomic source definition, so a later layer can replace `env` with `file` without retaining the old field. Strict structure, uniqueness, and cross-field rules are checked after all layers have produced the effective configuration.

For `[[files]]`, cdh treats redundant `/`, `.` path segments, and a trailing `/` as alternate spellings of the same directory. For example, `models//checkpoints/` is canonicalized to `models/checkpoints`. Use `target_dir = "."` or `target_dir = "./"` to place a file directly in the ComfyUI root. Empty and absolute directories, control characters, and every authored `..` segment remain invalid. An overlay for the same normalized target patches an item of the same `type`; changing `type` replaces the complete item so source-specific fields are not inherited.

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

## Set persistent image environment values

Use `[system.env]` for non-secret Docker `ENV` values that you need to add to or override in the selected base image. Do not repeat supported toolchain values that the base image already supplies. Configured values persist in the resulting image and may be visible in image metadata, history, or logs, so do not place secrets there; cdh-reserved runtime, path, and package-manager names remain unavailable for override.

When cdh installs application or custom-node packages, supported toolchain values from the effective build-container environment are available to package build steps. Existing controlled proxy handling and command-owned values remain separate; arbitrary entries do not become installer or Python configuration. See [Package-build environment](build-and-lock.md#package-build-environment) for the supported values and complete operational boundary.

## Choose application and tool capabilities

Manager and comfy-cli are independently controlled optional capabilities. Both are enabled when their switches are omitted, and either can be disabled separately:

- `comfyui.install_manager` controls the Manager capability from the selected ComfyUI checkout and its `cm-cli` Registry interface. Registry custom nodes require Manager. Enabling it does not add `--enable-manager` to ComfyUI runtime arguments.
- `comfyui.install_cli` controls the separately resolved user-facing comfy-cli tool. cdh does not use comfy-cli to build the image or install Registry nodes.

Entries in `python.uv_tools` request additional isolated command-line tools. They do not install packages into the ComfyUI application environment. See the [build and lock guide](build-and-lock.md) for package-source, resolution, and tool-environment behavior.

`python.extra_packages`, `python.uv_tools`, and `pytorch.extra_packages` accept standard named PEP 508 requirement forms parsed by `packaging.Requirement`: a distribution name with optional extras, standard version specifiers, and a marker; or a named direct reference written as `name[extras] @ URL`, also with an optional marker. Examples include `numpy>=2,<3`, `ruff==0.16.0rc1`, and this named wheel:

```toml
[pytorch]
extra_packages = ["sageattention @ https://github.com/jimlee2048/SageAttention/releases/download/v2.2.0/sageattention-2.2.0+cu130torch2.13-cp39-abi3-linux_x86_64.whl"]
```

cdh admits named direct references over `https`, `http`, `git+https`, and `git+http`. The URL must have a host, any authored port must be parseable, and username or password userinfo must be absent. A direct reference remains opaque after this structural check; uv decides whether the referenced wheel, source archive, or VCS project is installable for the selected target.

Markers are evaluated once against cdh's fixed build target: the configured CPython version on Linux `amd64`/`x86_64`. Host values are never used, and unavailable kernel release/version values are fixed empty strings. A declaration whose marker is inactive for that target is not resolved or installed. Marker variables that require unavailable package metadata or dependency-group context (`extra`, `extras`, and `dependency_groups`) are rejected with a diagnostic.

Unnamed bare URLs, local paths and `file:` URLs, editable requirements, raw pip/uv options, SSH transports including `git+ssh`, and every URL containing userinfo are unsupported. Use the required `name @ URL` form for a public remote source. Standard specifiers accepted by `packaging` are not narrowed by a cdh selector whitelist; this includes compatible-release and exclusion clauses, wildcard equality, arbitrary equality `===`, and prerelease, development, or local-version operands where the standard parser accepts them. cdh does not solve general range equivalence or pre-validate whether a selector has any release, and the resolver must still produce a canonical PEP 440 distribution version. Conflicting active declarations of the same normalized package are rejected within the application packages or isolated tools.

## Choose custom nodes and build hooks

Custom nodes may use either a Registry identity or a direct-Git URL. Registry nodes require Manager; direct-Git nodes do not. Mixed declarations retain their effective configuration order. Set `custom_nodes = []` in a later layer to remove inherited nodes.

Each node may name pre-install or post-install hooks. Hook paths are relative to the directory passed explicitly with `--build-hooks-dir`; there is no implicit hook root. The repository includes small [`pre.sh`](../../examples/build-hooks/pre.sh) and [`post.sh`](../../examples/build-hooks/post.sh) examples.

Build hooks and custom-node installers execute trusted user-selected code during the image build. Review them before use, and do not put secrets in hook files because their contents remain in the image and its layers.

## Add files during the image build

Every build file explicitly selects an HTTP or host-local source. The two variants share only their image target:

```toml
[[files]]
type = "http"
url = "https://example.test/model.safetensors"
target_dir = "models/checkpoints"
filename = "remote-model.safetensors"
downloader = "httpx"

[[files]]
type = "local"
path = "artifacts/model.safetensors"
target_dir = "models/checkpoints"
filename = "local-model.safetensors"
content_lock = false
```

HTTP files may also select `checksum` and `download_mode`. Local files instead use `path` and optional `content_lock`; they do not use a downloader or a hand-authored checksum. All build files are authoritative: a successful build places the declared content at the target, so build configuration has no `overwrite` field.

A relative local `path` uses the real parent directory of the first `-f` configuration file as its common base. Absolute paths and normalized parent traversal are accepted. The selected source must be one regular host file; cdh rejects observed symlinks, Windows junctions and other reparse points, directories, and special files. The locator is ordinary non-secret host input: it is not serialized into the lock, BuildPlan, rendered metadata, manifest, or image configuration, but it does not receive Secret-value handling or redaction.

`content_lock = false` is the default and avoids a cdh SHA-256 scan during planning. `content_lock = true` streams the source into a SHA-256 identity stored in the canonical lock and BuildPlan, then verifies that identity again while materializing the context. The [build and lock guide](build-and-lock.md#build-files-and-local-context-materialization) explains context materialization modes, `--check` cost, remote-builder transfer, and image placement. Local sources are build-only; only HTTP file declarations become baked runtime defaults.

## Authenticate HTTPX file downloads

Define a named Secret and reference it from a Bearer credential route. The token is always a whole-value Secret reference rather than an inline value:

```toml
[secrets.hf_read]
env = "HF_TOKEN"

[[cdh.downloader.credentials]]
match = "https://huggingface.co/acme/private-model/"
type = "bearer"
token = { secret = "hf_read" }

[[files]]
type = "http"
url = "https://huggingface.co/acme/private-model/resolve/main/model.safetensors"
target_dir = "models/checkpoints"
filename = "model.safetensors"
downloader = "httpx"
```

`match` defines a credential protection space from its exact scheme, case-normalized host, effective port, and raw path segments. The longest path-segment prefix wins for every actual outbound request, including redirects. Query parameters on the file URL remain unchanged but do not participate in route selection; a route itself cannot select by query. A redirect outside every route receives no cdh Bearer value, a redirect into another route uses that route's Secret, and an HTTPS route never authorizes an HTTP target unless that target has its own matching HTTP route. HTTP routes are permitted with a warning because the token then lacks TLS transport confidentiality.

The named source uses the same host env/file acquisition boundary described for Git Secrets below and is limited to 65,525 bytes. Supply the bare RFC 6750 `b64token` value without a `Bearer ` prefix or trailing newline. cdh does not trim it, decode it as Base64, parse JWT claims, require a provider-specific prefix, or contact the provider during admission.

Authenticated files must use the effective `httpx` downloader. cdh rejects an initial matching URL that selects aria2 because aria2 cannot enforce the same per-redirect credential scope; ordinary public downloads remain supported by either backend. cdh does not preflight a public aria2 URL to predict redirects, so a later protected endpoint simply follows aria2's normal HTTP failure path without receiving a token.

Downloader routes merge by canonical `match`: a later equivalent route atomically replaces the earlier one at its original position, a distinct route appends, and `credentials = []` clears inherited routes. Secret definitions merge independently by logical name. Build routes and Secret sources are build-only and are not baked into runtime configuration; deployments that need authenticated runtime downloads declare an independent route and container-visible Secret source in the mounted runtime config. See [Build and lock](build-and-lock.md#authenticated-httpx-file-downloads) and [Runtime](runtime.md#authenticated-httpx-downloads) for their separate delivery and lifetime boundaries.

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

Environment and file sources are resolved lazily within the command that needs them. Syntax-only validation never reads them, and a matching accepted lock can avoid a provider-time read. Relative file locators use the real parent of the first `-f` file as their common base; absolute and normalized parent-traversal paths are allowed. Secret files must be regular files rather than symlinks, and values are limited to 65,525 bytes. On POSIX, cdh warns about group or world permission bits. On Windows, restrict the source file's ACL yourself; cdh does not implement a general Windows access audit. Git passwords must also be non-empty and contain no NUL, carriage return, or newline; create token files without a trailing newline.

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
