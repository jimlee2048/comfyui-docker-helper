# Build and lock images

English | [简体中文](build-and-lock.zh-CN.md)

This guide covers local validation, canonical-lock reconciliation, rendered build contexts, and Docker image builds. Start with the [configuration guide](configuration.md) to choose and layer configuration files. The commands below assume your configuration is named `cdh.toml` and run from its directory.

Multi-line commands use POSIX shell continuation syntax. On Windows, enter them on one line or replace each trailing `\` with PowerShell's backtick continuation character.

## Host and target platforms

All `cdh host *` workflows run natively on supported Windows and Linux hosts. Install cdh with the ordinary [`uv tool` or `pip` command](../../README.md#install); the installer selects the required platform dependencies. Docker builds always target Linux `amd64`. A Windows host normally uses Docker Desktop running Linux containers with Buildx; another endpoint must provide equivalent Linux `amd64` Buildx behavior. cdh does not build or run Windows containers. `cdh container *` is for execution inside the generated Linux image.

Automated Windows validation covers native CLI, filesystem, Git, rendering, packaging, and Docker/Buildx adapter behavior. It does not run a real Docker Desktop build or prove Docker Desktop SSH-agent forwarding. Docker Desktop, builder, or agent-integration failures therefore retain the underlying Docker/BuildKit diagnostic.

cdh validates the file type and lexical path shape it observes while reading local Secret, hook, and build-file inputs, and rejects observed symbolic links, Windows junctions or other reparse points, and special files. Secret files additionally enforce the 65,525-byte limit. Hook files and content-locked local build files bind streamed source bytes to a digest and revalidate that digest before publication; unlocked local build files are still admitted and materialized without creating a cdh content digest. This is not isolation from another local process: do not allow an untrusted process to modify a selected input file or its directory concurrently with cdh.

## Validate, render, and build

Validate configuration before resolving or building anything:

```bash
cdh host validate -f cdh.toml
```

Validation is local: it makes no provider or Docker calls and writes no files. Repeat `-f/--file` to use configuration layers; cdh merges them in command-line order and validates the effective result.

Render a reusable build context and canonical lock:

```bash
cdh host render \
  -f cdh.toml \
  -o .cdh/build/current \
  --overwrite
```

Rendering reuses a matching lock. It may use Docker when a missing or changed image identity must be resolved. `--overwrite` replaces only an existing valid cdh-owned context; otherwise cdh refuses the replacement.

cdh prepares a complete replacement before changing an existing context. `--overwrite` is not crash-safe: a process or host interruption can leave the output missing while the previous complete context remains in a sibling backup. If a diagnostic reports a retained backup path, preserve that backup for manual recovery before retrying.

Build an image with Docker Buildx:

```bash
cdh host build \
  -f cdh.toml \
  --context-dir .cdh/build/current \
  -t my-comfy:dev \
  --load
```

`host build` renders the selected context and then invokes Buildx. Supply one or more `-t/--tag` values or configure `[build].tags`. Use `--load` for the local Docker image store or `--push` for a registry; the two options are mutually exclusive.

### Dynamic publication tags

Publication tags may interpolate the accepted ComfyUI identity with exactly these expressions:

- `${{ comfyui.release }}` is the normalized formal release without a leading `v`;
- `${{ comfyui.commit }}` is the full 40-character commit; and
- `${{ comfyui.commit.prefix(n) }}` uses `n` commit characters, where `n` is from 12 through 40.

Expressions are allowed only in an explicit tag component. A literal image name without a tag still means `latest`. Referencing `comfyui.release` is a hard error when the accepted identity has no formal release, including nightly and direct-commit builds; cdh neither skips that tag nor supplies a fallback. A `-t/--tag` list replaces `[build].tags` as a whole and accepts the same syntax. The resolved ordered targets must remain unique after familiar Docker-name normalization.

Tags and `[build].output` are process-local publication choices. They do not enter the canonical lock, BuildPlan, rendered context, final manifest, image-configuration digest, or image-content identity. Changing only these choices can therefore reuse the same rendered context and image build. Registry publication is not transactional: if a multi-tag `--push` fails partway through, inspect the registry and retry any missing targets.

### Reuse an external build cache

Use `--cache-from` to reuse an existing BuildKit cache and `--cache-to` to save the cache produced by the build:

```bash
cdh host build \
  -f cdh.toml \
  --context-dir .cdh/build/current \
  -t registry.example.com/my-comfy:dev \
  --push \
  --cache-from "type=registry,ref=registry.example.com/cache/my-comfy:build" \
  --cache-to "type=registry,ref=registry.example.com/cache/my-comfy:build,mode=max"
```

Each option accepts one Docker Buildx cache specification and may be used independently. Use any cache backend supported by the active Buildx builder. Authenticate through Docker or the backend's supported credential mechanism rather than placing credentials in the option value. See Docker's [cache backend documentation](https://docs.docker.com/build/cache/backends/).

## Private Git custom nodes over HTTPS

Configure Secret sources and `[[cdh.git.credentials]]` routes as described in [Supply private HTTP(S) Git credentials](configuration.md#supply-private-https-git-credentials). During a host command, cdh uses the selected route for direct-Git identity resolution and makes the effective route credentials available to BuildKit for custom-node installation and recursive submodules. Tokens are not placed in Git URLs or command arguments.

Secret handling is lazy and scoped to the command. cdh keeps source locators and resolved values out of durable build artifacts and its own diagnostics, and attempts cleanup when the command exits through supported success, error, or interruption paths. An ordinary cleanup failure is reported, but abrupt process or host termination cannot guarantee cleanup. This is a structural non-persistence boundary, not a sandbox: trusted custom-node hooks and installers can still read, print, or copy credentials available to their combined build step. An `http://` credential route is allowed but warns because it lacks TLS transport confidentiality.

On POSIX, an environment Secret preserves the environment value's raw bytes; on Windows, cdh encodes the Unicode environment value as UTF-8. File Secrets remain regular-file inputs with a 65,525-byte limit. cdh warns when POSIX group or world permission bits are present. On Windows, restrict the source file's ACL yourself because cdh does not implement a general Windows access audit. cdh-owned temporary Secret snapshots remain private through POSIX modes or a protected Windows DACL.

BuildKit does not include Secret contents in a `RUN` instruction's cache key; only the Secret ID and mount properties participate. Rotating a token can therefore reuse an already completed custom-node layer without contacting the current credential source. When building a rendered context directly, use Buildx `--no-cache`, or use other ordinary BuildKit cache controls, when a fresh authentication check is required. cdh deliberately does not hash a token into a cachebuster.

### Build a rendered HTTPS context directly

The rendered Dockerfile declares a stable, required Secret ID for each credential available to a direct-Git build. When invoking Buildx yourself, bind every declared ID to the corresponding value. For example, copying or uncommenting both complete private-HTTPS blocks in [`examples/full.toml`](../../examples/full.toml) and rendering that configuration produces these IDs:

```bash
docker buildx build \
  --secret "type=env,id=cdh-git-credential-github_pat,env=CDH_GITHUB_PAT" \
  --secret "type=file,id=cdh-git-credential-gitlab_pat,src=/path/to/gitlab-pat" \
  --load \
  -t my-comfy:dev \
  .cdh/build/current
```

The manual caller owns source admission and cleanup. Credential Secrets, SSH forwarding, and known-hosts Secrets are independent inputs and may be supplied together.

## Private Git custom nodes over SSH

Use `--ssh` when a direct-Git custom node or one of its recursive submodules needs an SSH identity from the host. Before building, load the required identity into the default SSH agent and add the server's host key to a default OpenSSH known-hosts file.

```bash
cdh host build \
  -f cdh.toml \
  --context-dir .cdh/build/current \
  -t my-comfy:dev \
  --load \
  --ssh
```

On POSIX, the option requires a non-empty `SSH_AUTH_SOCK`; cdh forwards that default agent and existing default user and system known-hosts files. On Windows, `SSH_AUTH_SOCK` is not a cdh prerequisite: cdh requests BuildKit's `--ssh default` and lets Docker/BuildKit select and validate the agent, while automatically supplying only existing user-level `~/.ssh/known_hosts` and `~/.ssh/known_hosts2` files. Windows system known-hosts discovery is not supported. cdh does not inspect the agent, add host keys, copy private keys, or read `~/.ssh/config` on either platform. For a self-hosted service or non-default port, use an explicit locator such as `ssh://git@example.test:2222/group/node.git` and add the corresponding host-and-port entry to a supported default known-hosts file. SSH aliases, `ProxyJump`, custom key/trust-file selectors, and raw key files are not supported. `--ssh` does not supply HTTPS tokens; configure those with `cdh.git.credentials`.

If the effective configuration has no direct-Git node, `--ssh` prints one warning and continues without forwarding anything. Otherwise, a missing POSIX agent fails before source resolution. On Windows, unavailable default-agent forwarding is reported by Docker/BuildKit; host-key, authentication, and Git/submodule failures also keep their underlying diagnostics.

Treat configured custom-node hooks and installers as trusted code: during custom-node installation they can use the forwarded agent and read the supplied trust files. Private key bytes remain in the agent, and cdh does not automatically place the agent or trust files in the rendered context or image. A cached custom-node layer may also be reused without contacting the current agent or rechecking updated trust; bypass the relevant BuildKit cache when a fresh authentication check is required.

### Build a rendered context directly

A rendered direct-Git context can also be built with Buildx. Supply the default SSH agent and a secret for each known-hosts file the build needs. This example uses the common user trust file:

```bash
docker buildx build \
  --ssh default \
  --secret "type=file,id=cdh-ssh-known-hosts-user,src=$HOME/.ssh/known_hosts" \
  --load \
  -t my-comfy:dev \
  .cdh/build/current
```

The rendered Dockerfile shows the stable secret ID for each other supported default trust file. Omit any secret whose source does not exist.

## Hook source directories

Build hooks referenced by custom-node configuration have no implicit source directory. Pass `--build-hooks-dir` to `validate`, `render`, and `build`:

```bash
cdh host validate \
  -f cdh.toml \
  --build-hooks-dir build-hooks
```

Paths in configuration are relative to this directory. cdh admits only the referenced regular `.sh` and `.py` files and preserves their safe relative layout. Build hooks are trusted code, and their verified source bytes remain in the final image and its layers. Do not put secrets in them. See the [build-hook examples](../../examples/build-hooks/).

Pass `--runtime-hooks-dir` to `render` or `build` to bake a runtime hook tree:

```bash
cdh host render \
  -f cdh.toml \
  -o .cdh/build/current \
  --build-hooks-dir build-hooks \
  --runtime-hooks-dir runtime-hooks \
  --overwrite
```

Only direct regular `.sh` and `.py` files under `pre-start.d/`, `post-start.d/`, and `stop.d/` are selected and baked. Other ordinary files and directories are ignored without recursion and produce aggregated warnings; unsafe filesystem entries and source inspection/read failures remain errors. Omitting the option bakes no runtime hook tree. Mounted runtime hooks are separate deployment-time inputs; see the [runtime guide](runtime.md) and [runtime-hook examples](../../examples/runtime-hooks/).

## Build files and local context materialization

Build `[[files]]` declarations are authoritative final image content. HTTP files are downloaded into staging and atomically replace the target after any configured checksum succeeds. Host-local files are materialized into a plan-owned `build/files/` context slot, then placed at the exact target with `COPY --link --chmod=0644`. Existing lower-image content does not suppress either operation, and build files have no `overwrite` setting. Every target remains a strict descendant of `COMFYUI_PATH`.

Set `[cdh].local_file_mode` to choose how local bytes enter the rendered context:

- `auto` is the default. It attempts a copy-on-write clone and falls back to streaming copy only when clone capability is unavailable or the filesystems do not support the operation.
- `clone` requires a supported copy-on-write clone and fails without publishing a context when it is unavailable.
- `copy` always performs a fixed-buffer streaming copy.

Neither clone mode uses a hardlink or symlink: the published context file is independent from later source changes. A clone can avoid physically copying unchanged extents on a capable local filesystem, but the complete file still belongs to the context. BuildKit must read it, and a remote builder must receive it, so `content_lock = false` does not eliminate context storage, builder-cache, or upload costs.

With `content_lock = false`, ordinary planning does not hash the source. An explicit `--check` first compares safe file shape and size, then streams byte equality only when sizes match. With `content_lock = true`, planning streams SHA-256 into the canonical lock and BuildPlan; materialization rehashes the source, and `--check` streams the context slot against that intended digest. These operations are bounded-memory but necessarily read the complete file when their result requires it.

Only HTTP build files are projected into `runtime/config.toml`. A local source locator is host-only and never becomes a runtime import instruction; deployment-time replacement remains the mounted runtime configuration's separate responsibility.

## Authenticated HTTPX file downloads

Downloader credential routes authorize only cdh's HTTPX file-download instruction. `validate` and `render` check route structure and Secret references without reading Secret values. When a build contains at least one effective HTTPX file, `host build` resolves the complete distinct Secret set referenced by the effective downloader routes before starting Buildx and grants it only to the file-download instruction. Redirects can therefore select another declared route, while installers, Git, hooks, and other build instructions receive no downloader credentials.

Route patterns and logical Secret names are ordinary build metadata. Secret source locators, resolved token values, generated authorization headers, and token digests are not persisted in cdh-owned locks, BuildPlans, context metadata, manifests, image metadata or history, or command output. Build routes and Secret definitions are not baked into runtime configuration; declare runtime routes and container-visible Secret sources independently when deployment-time downloads need authentication.

Building the rendered context manually bypasses the host Secret session. Supply each required file-backed mount shown by the rendered Dockerfile under its stable `cdh-downloader-credential-<name>` ID. Do not pass a token as a build argument or place it in the context.

BuildKit Secret contents do not ordinarily invalidate an instruction cache. Rotating a token can therefore reuse a completed download layer, and a cache hit does not prove that the current credential works. Use the ordinary BuildKit cache controls when the build must perform a fresh authenticated request; cdh deliberately does not add a token-derived cachebuster.

## From effective configuration to a context

cdh uses one forward-only planning flow:

```text
effective configuration -> canonical lock -> BuildPlan -> rendered context
```

The effective configuration describes intent. `config.lock.toml` records the accepted exact external identities, hook identities, and explicitly content-locked local file identities used for host reconciliation. cdh then constructs one immutable BuildPlan, which is the build-time execution authority. Context rendering projects that plan together with its exact wheel and admitted local inputs; build-time helpers do not re-read host configuration or the lock to make new planning decisions.

## Reconciliation modes

Provider policy and filesystem/build side effects are separate. Choose among these five user-facing modes:

| Mode | Resolution behavior | Context and build behavior |
| --- | --- | --- |
| Default | Reuse unchanged entries, resolve missing or changed inputs, and remove deleted identities. | Write the accepted lock and rendered context. |
| `--locked` | Require the existing lock and content-locked local inputs to match exactly; make no provider or Docker calls during reconciliation. | Compare the existing context and write nothing. Unlocked local source bytes are not compared; use `--check` for that explicit streamed comparison. `host build` still invokes Buildx after the checks pass. |
| `--upgrade-lock` | Refresh moving selectors while retaining unchanged exact selections. | Write the updated lock and rendered context. |
| `--check` | Apply default reconciliation policy. | Compare the complete expected context with the existing one; write nothing and do not build. |
| `--dry-run` | Use default policy unless combined with `--locked` or `--upgrade-lock`. | Print the exact BuildPlan plus a separate process-local `Buildx output` section containing the expanded mode and tags when applicable, or `None`; write nothing and do not build. |

`--check` cannot be combined with a lock-policy or dry-run modifier. `--locked` and `--upgrade-lock` are mutually exclusive. When `--dry-run` is combined with a lock policy, preview behavior replaces context comparison or publication. `Buildx output: None` means that this invocation has no publication output plan; it is not part of, or a missing field from, the BuildPlan.

No-write does not necessarily mean offline. Default, `--check`, and `--dry-run` may call providers and may require Docker when the current lock cannot supply a required image identity. A complete matching lock keeps those paths Docker-free. Only `--locked` forbids provider and Docker calls during reconciliation; Docker Buildx remains a separate requirement for `host build`.

Every target-active named direct Python reference is treated as moving, even when its URL text contains a version, hash fragment, or VCS ref. Default reconciliation and `--check` may reuse an unchanged matching result, while `--locked` requires an existing matching result and does not contact the source during host-side reconciliation. `--upgrade-lock` resolves every moving direct reference again. None of these modes turns a URL or VCS ref into an artifact lock, and a subsequent Buildx build may still need to fetch and install each active direct source.

Malformed or unsupported lock files fail closed with a diagnostic instructing you to remove and regenerate the lock.

## Rendered context

A rendered context contains:

- `.cdh-rendered`, the host marker for a cdh-owned context;
- `config.lock.toml`, host-only reconciliation state;
- `build-plan.json`, the canonical build-time execution plan, mounted read-only only while each owning build instruction runs;
- `bootstrap/comfyui_docker_helper-<version>-py3-none-any.whl`, the exact validated cdh wheel installed into the image;
- `build/hooks/`, containing only referenced verified build-hook bytes when configured;
- `build/files/`, containing plan-addressed independent copies or clones of configured host-local build files;
- `runtime/config.toml`, derived from the BuildPlan;
- `runtime/hooks/`, containing the verified baked runtime hook tree when configured;
- `Dockerfile`, rendered with literal digest-qualified base-image references; and
- `.dockerignore`, which excludes `config.lock.toml` and `.cdh-rendered` from Buildx input.

The context contains no root `config.toml`. Host-local source paths, Secret source locators, resolved Secret values, publication tags, and output selection are not BuildPlan inputs. The Dockerfile has no argument that can replace lock-authoritative image identities. The complete Plan remains in the host context and is available to the selected local or remote builder, but its per-instruction read-only mounts do not persist `/opt/cdh/build/build-plan.json` in the final image; the final manifest retains the Plan digest binding.

## Python environments and package sources

The image keeps application packages and user tools in separate ownership domains:

- `/opt/venv` contains ComfyUI, its application dependencies, and the optional checkout-owned Manager/`cm-cli` capability.
- cdh, optional `comfy-cli`, and each configured `[python].uv_tools` package use separate environments under `/opt/uv/tools`.
- tool commands are linked under `/opt/uv/bin`; executable ownership collisions fail instead of replacing an existing command.

`comfy-cli` is an optional user tool and is not used to install ComfyUI, Manager, or Registry custom nodes during the image build.

cdh-controlled index resolution and generic transitive dependencies use `[python].index_url` unless the package belongs to the PyTorch group. In that group, protected requirements from the selected ComfyUI checkout and every index-backed member use only the CUDA-derived PyTorch index; a missing index-backed member does not fall back to a same-named package on the ordinary index. A non-protected, target-active configured `name @ URL` member instead keeps its authored direct source and receives no PyTorch-index route. The group is resolved and installed atomically, its exact resulting top-level versions are verified, and the protected foundation cannot be replaced by a direct reference.

Each target-active direct requirement in `python.extra_packages` is preserved for application installation while `[python].index_url` remains available for index-backed and transitive dependencies. Each active `python.uv_tools` requirement is installed under its own `/opt/uv/tools/<name>` environment with the managed Python interpreter; a direct tool keeps its authored source while transitive dependencies retain the default Python index. Installation never adds a downloader, URL rewriting, or a second package path.

During custom-node installation, both Registry Manager and the Direct-Git Python installation processes for root requirements and `install.py` receive the BuildPlan-owned ordinary and CUDA-derived PyTorch indexes. Runtime constraints retain the exact PyTorch group and the selected torch wheel's setuptools compatibility; the isolated-build projection retains the exact PyTorch group without itself adding that runtime-only setuptools range. A package manager may also apply its ordinary runtime-constraint behavior inside build isolation. uv considers compatible candidates from both indexes, consistent with the shared pip path, so an ordinary package name on the CUDA index does not hide a compatible version on the ordinary index. This lets isolated package builds resolve against the protected application foundation, but it does not turn every dependency selected by a trusted installer into a cdh lock or BuildPlan input. Manager owns node-specific Registry installation effects; for Direct-Git nodes, cdh verifies the exact root commit and recursive gitlinks and admits the root requirements, while dependency installation and `install.py` effects remain trusted execution.

cdh records and verifies the exact resolved top-level package version, but it does not lock the bytes or VCS commit behind a direct source. The image build installs the source that was configured and fails if the resulting package name or version does not match the resolved result.

Package direct references are ordinary public configuration, not Secret locators. Active references become rendered build inputs and may be visible to the builder or build cache; configured direct uv-tool references may also appear in image history. URL userinfo is rejected, and cdh does not attach downloader/Git credential routes to package installation. Never put tokens or private credentials in these references.

## Final evidence and replay boundary

After all image mutations succeed, cdh writes the strict final-state observation `/opt/cdh/build/manifest.json`. It binds the image-configuration, canonical lock, and BuildPlan digests and records intended and observed direct identities. The manifest is evidence, not another resolver, lock, replay input, support verdict, or general service-health check.

cdh provides bounded verified replay of cdh-controlled direct inputs. For a target-active package direct reference, the replay identity is the authored request plus the exact installed top-level distribution version, not the fetched artifact: unchanged content at a URL is not proved, and a moving VCS ref is not pinned to an observed commit. A moving direct or VCS dependency selected by Registry Manager or a Direct-Git install script is likewise not independently locked or attested by cdh. `--locked` avoids source contact only during host-side reconciliation; a subsequent Buildx build may still need to fetch and install that active authored source. This does not promise an offline or byte-identical build, a complete lock of transitive dependencies or every fetched artifact, authenticity for package or file downloads without a user-supplied hash/checksum, deterministic effects from trusted installers or hooks, or replay of deployment-time mutations.
