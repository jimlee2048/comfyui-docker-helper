# Configuration

English | [简体中文](configuration.zh-CN.md)

Start here when choosing what goes into your image. Use the [build guide](build-and-lock.md) to build it and the [runtime guide](runtime.md) to change deployment settings without rebuilding.

## Choose a starting example

Save [`minimal.toml`](../../examples/minimal.toml) as `cdh.toml`. Add only the sections you need from [`full.toml`](../../examples/full.toml), the annotated field reference. The full example includes illustrative paths and hooks; it is not a ready-to-build profile. Check the comments in the full example for defaults.

```bash
cdh host validate -f cdh.toml
```

Validation checks configuration without network access, Docker, file writes, or reading local sources and credentials. Pass `--build-hooks-dir` when referencing build hooks. For exact field syntax, consult the full example and `cdh host validate --help`.

## Layer configuration

Keep shared choices in `cdh.toml` and put local changes in a second file. For example, save this as `local.toml`:

```toml
files = []

[comfyui]
install_cli = false
custom_nodes = []
```

This removes inherited files and nodes and disables comfy-cli, while leaving other settings intact:

```bash
cdh host validate -f cdh.toml -f local.toml
```

Repeat the same ordered `-f` arguments when building. Later files take precedence, with these exceptions to ordinary replacement:

| Setting | How layers combine | How to clear it |
| --- | --- | --- |
| Tables and scalar values | Tables merge recursively; later scalar values replace earlier ones | Set the desired value explicitly |
| Ordinary arrays, including tags, hook lists, runtime arguments and SSH public keys | The later list replaces the whole list | `[]`, if the resulting configuration permits an empty list |
| OS and Python package lists | New entries append; repeated equivalent entries across layers are kept once | `[]` |
| Custom nodes | Entries match by Registry ID, exact Git URL, or local target directory and overlay at their original position | `custom_nodes = []` |
| Files | Entries match by normalized `target`; the same `type` patches fields, a changed `type` replaces the whole entry | `files = []` |
| Credential routes | Equivalent `match` values replace the whole route at its original position | `credentials = []` |
| Named secrets | A later definition replaces that secret's complete source, so `file` can replace `env` | Replace the source definition |

Some lists must remain non-empty, such as the effective build platform list. Duplicate entries within one layer may be invalid even when repetition across layers is supported.

Python packages are deduplicated only when the complete normalized requirement matches. Adding a different version constraint does **not** override an earlier requirement for the same package: conflicting requirements active for the target fail validation. To replace the package set, clear it in a layer before adding a new list in a subsequent layer. Registry IDs match without regard to case, but punctuation variants remain different resources.

Relative local files, local nodes and secret files all use the real parent directory of the **first** configuration file as their base, including declarations in later layers.

## Select versions and packages

Select CUDA, PyTorch and ComfyUI explicitly. The image target is Linux `amd64` with CUDA. Python must be an exact stable patch within the package's supported range; see the [full example](../../examples/full.toml) for current defaults and supported selections.

ComfyUI accepts a release, a supported version constraint, `latest`, `nightly`, or a full lowercase commit. It is resolved from the official repository and must descend from the supported v0.11.0 floor. Unsupported versions and upstream image tags fail rather than silently selecting a substitute.

| You want to add | Configuration |
| --- | --- |
| OS packages | `system.extra_packages` |
| Packages used by ComfyUI | `python.extra_packages` |
| Packages from the CUDA-specific PyTorch source | `pytorch.extra_packages` |
| Separate command-line tools | `python.uv_tools` |

Python lists accept named requirements such as `numpy>=2,<3` or `package @ https://example.com/package.whl`. Optional extras and target markers are supported. Markers use the configured Python on Linux `amd64`, not the host; inactive requirements are skipped.

Use public HTTP(S) or Git-over-HTTP(S) references. Bare URLs, local paths, editable installs, SSH sources, raw installer options and URL credentials are not accepted. Metadata-dependent markers such as `extra` are also unavailable. A direct URL is visible to the builder and may appear in image history; never put a token in it. Download and Git credential routes do not authenticate package installation.

See [Python environments and package sources](build-and-lock.md#python-environments-and-package-sources) for source selection and compilation requirements.

## Choose custom nodes and build hooks

Manager and comfy-cli are enabled by default but can be disabled independently:

- `comfyui.install_manager` provides Manager and `cm-cli` from the selected ComfyUI checkout. Registry nodes require it. Enabling it does not add `--enable-manager` to runtime arguments.
- `comfyui.install_cli` installs comfy-cli as a separate user tool. cdh does not use it to build the image.

| Node source | Choose it when | Installation |
| --- | --- | --- |
| `registry` | The node is published in Comfy Registry | Manager installs the selected version |
| `git` | You need a repository branch, tag or commit | cdh checks out the locked commit and recursive submodules, then processes root `requirements.txt` and `install.py` if present |
| `local` | You are developing or supplying a local node | cdh copies selected source files, then processes the same optional installation files |

The [full example](../../examples/full.toml) shows each declaration. Nodes run in configuration order. Git and local nodes do not require Manager or comfy-cli, and cannot share a target directory.

A local node requires a real source directory and a safe `target_dir` directly under `custom_nodes`. It need not be a Git checkout. Only its root `.dockerignore` filters the copied tree, using Docker SDK matching; nested ignore files are ordinary files. Missing or empty rules select the whole tree. Selected links, special files, unsafe names, or source/output overlap are rejected. Empty selection is allowed with a warning. The root `.dockerignore` is retained; its bytes count toward `content_lock` when enabled.

Build hooks run with the build step's permissions. Use scripts you trust:

| Stage | When it runs |
| --- | --- |
| `pre_clone_hooks` | Git nodes only, before cdh creates and clones the target |
| `pre_install_hooks` | After Git checkout or local copy, before reading installation files; for Registry nodes, before Manager installation |
| `post_install_hooks` | After successful installation and checks |

Hooks still run when optional installation files are absent. Shell hooks use Bash; Python hooks use the application interpreter. Hooks start in the ComfyUI directory, while `install.py` starts in the node directory. Reference `.sh` or `.py` paths relative to an explicit `--build-hooks-dir`; see [hook source directories](build-and-lock.md#hook-source-directories).

## Add files during the image build

For example, place a model and a workflow directory into the image:

```toml
[[files]]
type = "local"
source = "artifacts/model.safetensors"
target = "models/checkpoints/model.safetensors"
content_lock = false

[[files]]
type = "local"
source = "artifacts/workflows"
target = "user/default/workflows"
content_lock = false
```

| Source | Meaning of `target` | What is copied |
| --- | --- | --- |
| HTTP URL | Exact file path relative to `COMFYUI_PATH` | Downloaded file; no filename is inferred from the URL |
| Local file | Exact file path relative to `COMFYUI_PATH` | That file |
| Local directory | Destination directory relative to `COMFYUI_PATH`; `.` is allowed | All contents, including hidden and empty directories, without the source basename |

Directories overlay existing content: unrelated image files remain. File/directory conflicts fail. Build files apply after node installation and hooks; overlaying a node does not rerun installation. Build declarations have no `overwrite` option; [runtime downloads](runtime.md#files-downloads-and-persistent-state) have a separate overwrite policy.

Local sources must be real files or directories. Links, junctions, special files and source/output overlap are rejected. Unlike local nodes, file-directory sources do not use `.dockerignore`. An empty directory still creates its target and produces a preparation warning. Validation does not inspect the source.

Targets use `/`, cannot overlap, and cannot contain `..`, backslashes, controls or a trailing slash. Redundant `/` and `.` segments normalize to the same target. No destination component or copied tree-member name may be `.cdh-staging` or start with `.wh.`; rename such members. HTTP and local-file targets must name files strictly below `COMFYUI_PATH`.

Image directories use mode `0755` and files `0644`; host ownership, timestamps, ACLs, extended attributes and executable bits do not define image permissions. Choose [copying and content checks](build-and-lock.md#build-files-and-local-context-materialization) separately from source/target placement.

Only HTTP file declarations also become runtime download defaults. Local sources are build-only; use deployment mounts to provide files at runtime.

## Supply credentials

Keep credential values outside TOML. Define a named source and reference it from a route:

```toml
[secrets.github_pat]
env = "CDH_GITHUB_PAT"

[[cdh.git.credentials]]
match = "https://github.com/example-private/"
username = "x-access-token"
password = { secret = "github_pat" }
```

A source selects exactly one `env` or `file`. A file source can be written as `file = "secrets/github-pat"`; create a regular file without a trailing newline and restrict its permissions or Windows ACL. Host secret files cannot be symlinks. Values are limited to 65,525 bytes; Git passwords must be non-empty and contain no NUL, carriage return or newline. Prefer read-only, repository-limited, expiring credentials.

Routes match scheme, host, port and a path-segment prefix; the longest matching path wins. Use HTTPS: HTTP routes warn because they expose credentials in transit. Git applies its own URL rewrites and redirects; check those settings when restricting credential use. Consult the credential provider for the required username.

### Authenticate HTTPX file downloads

For a private model, add a Bearer route and explicitly select HTTPX:

```toml
[secrets.hf_read]
env = "HF_TOKEN"

[[cdh.downloader.credentials]]
match = "https://huggingface.co/acme/private-model/"
type = "bearer"
token = { secret = "hf_read" }

[[files]]
type = "http"
source = "https://huggingface.co/acme/private-model/resolve/main/model.safetensors"
target = "models/checkpoints/model.safetensors"
downloader = "httpx"
```

Supply the bare Bearer token without a `Bearer ` prefix, whitespace or trailing newline. HTTPX selects a route for each request, including redirects: leaving all routes removes the cdh token; entering another uses its token. Query strings do not select routes. An HTTPS route does not authorize an HTTP destination.

aria2 cannot enforce this credential scope. An initially protected URL using aria2 is rejected; a public aria2 request that later redirects to a protected endpoint receives no cdh token.

Build credentials are not baked into runtime settings. Follow [build credential handling](build-and-lock.md#private-git-custom-nodes-over-https) for build access and cache behavior, or [runtime credential rotation](runtime.md#authenticated-httpx-downloads) for an independently configured container source. Hooks and installers are trusted code and can access credentials granted to their build step.

## Set image defaults

Use `[system.env]` only for non-secret persistent environment values:

```toml
[system.env]
TZ = "Etc/UTC"
```

These values may appear in image metadata, history or logs. Omit `TZ` to preserve the base image's behavior, or override it at container startup. Use the dedicated configuration fields for cdh's runtime paths and package-manager settings.

Use `[cdh.logs]` to set recording defaults; see [runtime logs](runtime.md#read-and-retain-logs). Runtime settings, SSH public keys, deployment mounts and their update rules are covered in the [runtime guide](runtime.md).
