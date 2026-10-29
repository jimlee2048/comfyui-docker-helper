# Build and lock images

English | [简体中文](build-and-lock.zh-CN.md)

Use this guide to build an image from `cdh.toml`, decide when to reuse or refresh locked inputs, and prepare local or private sources. For your first container, continue to [Start and stop ComfyUI](runtime.md#start-and-stop-comfyui).

## Validate, render, and build

Validate your configuration, then build and load an image into the local Docker image store:

```bash
cdh host validate -f cdh.toml
cdh host build -f cdh.toml -t my-comfy:dev --load
```

Validation is local and writes nothing. Building prepares a context, resolves inputs as needed and invokes Docker Buildx. Repeat the same `-f` layers and hook-directory options on both commands.

To keep the context at a chosen location:

```bash
cdh host render -f cdh.toml -o .cdh/build/current --overwrite
cdh host build -f cdh.toml --context-dir .cdh/build/current -t my-comfy:dev --load
```

`render` prepares the context and lock without building the final image. It can still require network access and Docker to resolve inputs. `build` also prepares a context; you do not need to run `render` first.

Use `--load` for local use or `--push` for registry publication; they are mutually exclusive. Supply tags with `-t` or `[build].tags`. After a successful local build, use the same image tag in the runtime guide.

`--overwrite` replaces only a valid cdh-owned context. cdh prepares a complete replacement first, but interruption during replacement can leave the output absent and the old context in a sibling backup. If an error reports a retained backup, preserve it for recovery before retrying.

### Output detail and streams

Put detail options before `host`, for example `cdh -v host validate -f cdh.toml`.

| Option | Effect |
| --- | --- |
| Normal | Useful progress and results; successful non-interactive validation may be silent |
| `-q` | Hide cdh informational output; keep warnings, errors and explicit data |
| `-v` / `-vv` | Add operational / debug detail |

Quiet and verbose cannot be combined. Results and BuildKit output use stdout; cdh progress and diagnostics use stderr. Redirected output is plain text. Quiet does not suppress BuildKit's output or child-process output.

## Host and target platforms

| Where you run cdh | What it supports |
| --- | --- |
| Native Linux x86_64/ARM64, Windows x86_64, macOS ARM64 | Host commands on the supported Python minors |
| Docker Engine | Must be able to run Linux `amd64` resolver containers |
| Docker Buildx | Must be able to build Linux `amd64` images |
| Resulting image | Linux `amd64` with CUDA; see [GPU runtime requirements](runtime.md#gpu-host-requirements) |

An ARM Engine needs amd64 emulation or an amd64 endpoint. Selecting a remote Buildx builder alone does not provide the Engine execution capability needed for resolution. Windows normally uses Docker Desktop in Linux container mode; Windows containers are unsupported.

Native host tests do not prove end-to-end Docker Desktop builds or SSH-agent forwarding. Integration failures retain Docker/BuildKit diagnostics; see the [testing handbook](../../tests/README.md#platform-coverage) for the tested host matrix.

Use real local source paths, including ancestors: symlinks, junctions and special files are rejected. On macOS, use `/private/tmp` and `/private/var` rather than their usual symlink aliases. Do not let untrusted processes modify selected inputs while cdh reads them.

## Reconciliation modes

`host render` supports all modes below. `host build` supports default, `--locked` and `--upgrade-lock`; use render for checks and previews.

Choose a mode by the result you need:

| Need | Mode | Result |
| --- | --- | --- |
| Build after editing configuration | Default | Reuse unchanged locked inputs; resolve new or changed inputs and remove deleted entries; write the lock/context |
| Require an already prepared matching context | `--locked` | Require matching locked inputs and context; do not write or resolve through providers. `host build` still runs Buildx afterward |
| Refresh moving selections | `--upgrade-lock` | Resolve moving selections again, retain unchanged exact selections, write the updated lock/context |
| Check whether a context matches | `--check` | Use normal resolution policy, compare complete expected structure and local file bytes; write nothing and do not build |
| Preview preparation | `--dry-run` | Inspect inputs and print a plan and publication choices; write nothing and do not build |

`--locked` and `--upgrade-lock` are mutually exclusive. `--check` cannot be combined with either or with `--dry-run`. Dry-run can use either lock policy; its preview replaces comparison or publication.

**No writes does not mean offline.** Default, check and dry-run may contact providers or use Docker when required identities are missing from the lock. A complete matching lock avoids Docker during those preparation paths. Only `--locked` forbids provider and Docker calls during reconciliation; building the image remains a separate Docker operation and can download dependencies.

For local content, `--locked` checks structure and opted-in content digests but does not compare unlocked source bytes. Use `--check` for that comparison. A malformed or unsupported lock fails with instructions to remove and regenerate it.

### What a lock does and does not preserve

The lock records selected direct inputs, such as resolved versions, Git commits and opted-in local content. Moving package URLs are refreshed by `--upgrade-lock` even if the URL text contains a version or commit. Locking a direct package URL records its request and resolved top-level version, not its artifact bytes or an observed VCS commit.

A matching lock does not guarantee an offline or byte-identical build, complete transitive dependency locking, deterministic hooks/installers, or deployment-time state. The final `/opt/cdh/build/manifest.json` records checks performed after image construction; it is not a health check or another input to build from.

## Build files and local context materialization

Define source placement in [Configuration](configuration.md#add-files-during-the-image-build). Prepared local files and nodes are independent of their sources, so a context can be built without the original directories. After editing source content, prepare the context again.

### Choose a copy mode

Keep `[cdh].local_file_mode = "auto"` unless you need a specific copying method. This setting applies to local files, directory contents and local custom nodes.

| Mode | When to use it | If fast cloning is unavailable |
| --- | --- | --- |
| `auto` (default) | Normal builds: try a fast copy-on-write clone | Use ordinary copying |
| `clone` | Require a copy-on-write clone | Fail without publishing the build context |
| `copy` | Always copy the file contents | No clone is attempted |

Both methods leave the source unchanged and produce independent files: later edits to the source do not change the prepared context. Cloning can reduce copying and additional disk space, but Docker still reads the files and a remote builder still receives them.

On macOS:

- Fast cloning works on compatible volumes. Cross-volume copies use ordinary copying in `auto` mode.
- Read-only sources are supported. Files marked immutable or append-only use ordinary copying in `auto` mode, because a clone would inherit restrictions that prevent normal handling or cleanup.
- Clones retain extended attributes such as download markers (`quarantine`), Finder information and resource forks. Ordinary copying may produce different metadata.

Permission errors, a full disk and other I/O failures stop the operation; they do not trigger an ordinary-copy retry.

### Content checks

| Choice | Cost and guarantee |
| --- | --- |
| `content_lock = false` (default) | Ordinary planning inspects structure without hashing source files; later copy, comparison or Docker use still reads them |
| `content_lock = true` | Reads and hashes selected content, verifies it during preparation, and verifies build-file content in the final image |
| `--check` | Streams source/context bytes for comparison, including unlocked files |

Hashing and comparison use bounded memory but still read complete files. An unlocked dry-run or successful fast clone does not prove every data block is readable; a later read can fail. Incomplete preparation is not published.

For local nodes, locking covers the selected input, including the root `.dockerignore`, not the installed tree after hooks and installers modify it. Excluded content does not affect the digest. See [local node selection](configuration.md#choose-custom-nodes-and-build-hooks).

## Private Git custom nodes over HTTPS

Declare routes and secret sources in [Configuration](configuration.md#supply-credentials). cdh uses them for host Git resolution and makes every distinct secret referenced by effective Git routes available to the combined custom-node build step when any direct-Git node is present, including for recursive submodules.

Hooks and installers in that step are trusted: they can read or disclose its credentials. cdh keeps resolved values and source locations out of its own durable build artifacts and diagnostics, but does not redact arbitrary child output. Normal command exit attempts temporary-secret cleanup; abrupt termination cannot guarantee it.

Rotating a token does not invalidate an already cached BuildKit instruction. A cache hit therefore does not prove the new credential works. Use ordinary BuildKit cache controls, such as `--no-cache` on a manual Buildx build, when a fresh authenticated operation is required.

## Private Git custom nodes over SSH

Load the required identity into the default SSH agent and add the server's host key to a default known-hosts file, then build:

```bash
cdh host build -f cdh.toml -t my-comfy:dev --load --ssh
```

| Host | Agent and trust files |
| --- | --- |
| POSIX | Requires `SSH_AUTH_SOCK`; forwards the default agent and existing default user/system known-hosts files |
| Windows | Docker/BuildKit selects the default agent; cdh supplies existing user `~/.ssh/known_hosts` and `known_hosts2`, not system trust files |

cdh does not load keys, add host keys or copy `~/.ssh/config`. Use explicit hosts and ports, such as `ssh://git@example.test:2222/group/node.git`, with matching known-hosts entries. SSH aliases, `ProxyJump`, custom key/trust-file selectors and raw key files are not supported. HTTPS credentials are configured separately.

Without a direct-Git node, `--ssh` warns and does nothing. Authentication, host-key and submodule errors retain Git/OpenSSH diagnostics. Hooks and installers in the custom-node step can use the forwarded agent and read trust files. Private key bytes remain agent-owned. Cached steps may skip authentication; bypass their BuildKit cache when checking changed access.

## Authenticated HTTPX file downloads

Configure [HTTPX routes](configuration.md#authenticate-httpx-file-downloads). Validation and rendering check references without reading token values. If a build includes any effective HTTPX file, `host build` resolves every distinct secret referenced by effective downloader routes and grants them only to the file-download step.

Build credentials are not runtime credentials. Configure an independent container-visible source for [runtime downloads](runtime.md#authenticated-httpx-downloads). As with Git credentials, token rotation does not invalidate a cached download layer.

## Build a prepared context directly

For a public context:

```bash
docker buildx build --load -t my-comfy:dev .cdh/build/current
```

Copy the **whole** prepared directory when moving a context. Keep its metadata and generated files intact; editing generated inputs can invalidate checks. The builder can read context inputs, so protect access to that directory and its cache.

A manual build bypasses cdh's host credential session. Supply the secret IDs declared by its Dockerfile; the caller owns source handling and cleanup. For example, a rendered Git route referencing `github_pat` uses:

```bash
docker buildx build \
  --secret "type=env,id=cdh-git-credential-github_pat,env=CDH_GITHUB_PAT" \
  --load -t my-comfy:dev .cdh/build/current
```

Downloader routes use file-backed mounts named `cdh-downloader-credential-<name>`. Never pass tokens as build arguments or place them in the context. For SSH, add `--ssh default` and the required trust-file secret, for example `--secret "type=file,id=cdh-ssh-known-hosts-user,src=$HOME/.ssh/known_hosts"` in a POSIX shell. The Dockerfile lists other supported IDs; omit trust-file mounts whose source does not exist. SSH and HTTPS inputs can coexist.

The [developer context layout](../dev/contracts.md#rendered-context-layout) describes internal files when debugging generated output.

## Hook source directories

Build hooks have no implicit source directory. Pass the same root to validation, rendering and building:

```bash
cdh host validate -f cdh.toml --build-hooks-dir build-hooks
cdh host build -f cdh.toml -t my-comfy:dev --load --build-hooks-dir build-hooks
```

Only referenced regular `.sh` and `.py` files are included, preserving safe relative paths. Their source remains in the image and its layers; do not put secrets in scripts. See [build-hook examples](../../examples/build-hooks/).

Use `--runtime-hooks-dir runtime-hooks` with render/build to bake runtime scripts. Only direct `.sh` and `.py` files in `pre-start.d`, `post-start.d` and `stop.d` are selected. Other ordinary entries are ignored with warnings; unsafe entries and read failures are errors. Omission bakes no hooks. Mounted runtime hooks are independent; see [runtime hooks](runtime.md#runtime-hooks-and-startup-readiness).

## Tags and external cache

A `-t` list replaces `[build].tags` as a whole. Tags may use these expressions in their tag component:

| Expression | Value |
| --- | --- |
| `${{ comfyui.release }}` | Accepted formal release without `v`; fails for an identity without a release |
| `${{ comfyui.commit }}` | Full commit |
| `${{ comfyui.commit.prefix(n) }}` | First 12–40 commit characters |

An image name without a tag means `latest`. Resolved targets must be unique. Changing tags or load/push selection does not change image content or invalidate the prepared context. Multi-tag push is not transactional: inspect registry state after a partial failure.

Use your builder's cache backend with `--cache-from` and `--cache-to`:

```bash
cdh host build -f cdh.toml -t registry.example.com/my-comfy:dev --push \
  --cache-from "type=registry,ref=registry.example.com/cache/my-comfy:build" \
  --cache-to "type=registry,ref=registry.example.com/cache/my-comfy:build,mode=max"
```

Either option can be used independently. Authenticate through Docker or the cache backend, not credentials embedded in option values. Docker owns cache backend compatibility.

## Python environments and package sources

ComfyUI and its application dependencies live in `/opt/venv`. cdh, comfy-cli and configured uv tools use separate environments under `/opt/uv/tools`, with commands on `/opt/uv/bin`. Command-name collisions fail instead of replacing another tool.

Ordinary packages and generic transitive dependencies use `[python].index_url`. Index-backed PyTorch-group packages use the CUDA-derived PyTorch index without fallback to the ordinary index. Supported non-protected direct references retain their authored source; protected foundation packages cannot be replaced that way.

Direct-source installation verifies the resolved top-level package name and version, not fetched artifact identity. Dependencies or mutations introduced by trusted node installers are outside the complete lock guarantee.

### Package-build environment

Choose a base image with the compilers, headers and libraries your packages need. Making compiler settings available does not install missing development tools.

Package installation receives supported proxy settings and a controlled subset of toolchain values from the base image and `[system.env]`. Do not repeat values already supplied by the base image. Arbitrary `PATH`, pip, uv or Python configuration cannot override cdh's managed interpreter, sources or constraints. See the [package-source contract](../dev/contracts.md#python-and-pytorch-package-source-ownership) when changing the implementation.
