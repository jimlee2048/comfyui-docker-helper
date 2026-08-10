# Build and lock images

English | [简体中文](build-and-lock.zh-CN.md)

This guide covers local validation, canonical-lock reconciliation, rendered build contexts, and Docker image builds. Start with the [configuration guide](configuration.md) to choose and layer configuration files. The commands below assume your configuration is named `cdh.toml` and run from its directory.

Multi-line commands use POSIX shell continuation syntax. On Windows, enter them on one line or replace each trailing `\` with PowerShell's backtick continuation character.

## Host and target platforms

All `cdh host *` workflows run natively on supported Windows and Linux hosts. Install cdh with the ordinary [`uv tool` or `pip` command](../../README.md#install); the installer selects the required platform dependencies. Docker builds always target Linux `amd64`. A Windows host normally uses Docker Desktop running Linux containers with Buildx; another endpoint must provide equivalent Linux `amd64` Buildx behavior. cdh does not build or run Windows containers. `cdh container *` is for execution inside the generated Linux image.

Automated Windows validation covers native CLI, filesystem, Git, rendering, packaging, and Docker/Buildx adapter behavior. It does not run a real Docker Desktop build or prove Docker Desktop SSH-agent forwarding. Docker Desktop, builder, or agent-integration failures therefore retain the underlying Docker/BuildKit diagnostic.

cdh validates the file type and lexical path shape it observes while reading local Secret and hook inputs, and rejects observed symbolic links, Windows junctions or other reparse points, and special files. Secret files additionally enforce the 65,525-byte limit. Hook files bind the bytes read during identity planning to a digest and revalidate that digest before materialization. This is not isolation from another local process: do not allow an untrusted process to modify a selected input file or its directory concurrently with cdh.

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

On POSIX, an environment Secret preserves the environment value's raw bytes; on Windows, cdh encodes the Unicode environment value as UTF-8. File Secrets remain regular-file inputs with a 65,525-byte limit. cdh warns when POSIX group or world permission bits are present. Windows source ACLs cannot be reduced to that POSIX check, so cdh emits a warning that source-file permissions could not be verified; restrict the source file's Windows ACL yourself. cdh-owned temporary Secret snapshots remain private through POSIX modes or a protected Windows DACL.

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

The runtime hook directory may contain only supported hook files directly under `pre-start.d/`, `post-start.d/`, and `stop.d/`. Omitting the option bakes no runtime hook tree. Mounted runtime hooks are separate deployment-time inputs; see the [runtime guide](runtime.md) and [runtime-hook examples](../../examples/runtime-hooks/).

## From effective configuration to a context

cdh uses one forward-only planning flow:

```text
effective configuration -> canonical lock -> BuildPlan -> rendered context
```

The effective configuration describes intent. `config.lock.toml` records the accepted exact external and local content identities used for host reconciliation. cdh then constructs one immutable BuildPlan, which is the build-time execution authority. Context rendering projects that plan together with its exact wheel and verified hook inputs; build-time helpers do not re-read host configuration or the lock to make new planning decisions.

## Reconciliation modes

Provider policy and filesystem/build side effects are separate. Choose among these five user-facing modes:

| Mode | Resolution behavior | Context and build behavior |
| --- | --- | --- |
| Default | Reuse unchanged entries, resolve missing or changed inputs, and remove deleted identities. | Write the accepted lock and rendered context. |
| `--locked` | Require the existing lock and local inputs to match exactly; make no provider or Docker calls during reconciliation. | Compare the existing context and write nothing. `host build` still invokes Buildx after the checks pass. |
| `--upgrade-lock` | Refresh moving selectors while retaining unchanged exact selections. | Write the updated lock and rendered context. |
| `--check` | Apply default reconciliation policy. | Compare the complete expected context with the existing one; write nothing and do not build. |
| `--dry-run` | Use default policy unless combined with `--locked` or `--upgrade-lock`. | Print the exact BuildPlan plus a separate process-local `Buildx output` section containing the expanded mode and tags when applicable, or `None`; write nothing and do not build. |

`--check` cannot be combined with a lock-policy or dry-run modifier. `--locked` and `--upgrade-lock` are mutually exclusive. When `--dry-run` is combined with a lock policy, preview behavior replaces context comparison or publication. `Buildx output: None` means that this invocation has no publication output plan; it is not part of, or a missing field from, the BuildPlan.

No-write does not necessarily mean offline. Default, `--check`, and `--dry-run` may call providers and may require Docker when the current lock cannot supply a required image identity. A complete matching lock keeps those paths Docker-free. Only `--locked` forbids provider and Docker calls during reconciliation; Docker Buildx remains a separate requirement for `host build`.

Malformed or unsupported lock files fail closed with a diagnostic instructing you to remove and regenerate the lock.

## Rendered context

A rendered context contains:

- `.cdh-rendered`, the host marker for a cdh-owned context;
- `config.lock.toml`, host-only reconciliation state;
- `build-plan.json`, the canonical build-time execution plan;
- `bootstrap/comfyui_docker_helper-<version>-py3-none-any.whl`, the exact validated cdh wheel installed into the image;
- `build/hooks/`, containing only referenced verified build-hook bytes when configured;
- `runtime/config.toml`, derived from the BuildPlan;
- `runtime/hooks/`, containing the verified baked runtime hook tree when configured;
- `Dockerfile`, rendered with literal digest-qualified base-image references; and
- `.dockerignore`, which excludes `config.lock.toml` and `.cdh-rendered` from Buildx input.

The context contains no root `config.toml`. Host-local source paths, Secret source locators, resolved Secret values, publication tags, and output selection are not BuildPlan inputs. The Dockerfile has no argument that can replace lock-authoritative image identities.

## Python environments and package sources

The image keeps application packages and user tools in separate ownership domains:

- `/opt/venv` contains ComfyUI, its application dependencies, and the optional checkout-owned Manager/`cm-cli` capability.
- cdh, optional `comfy-cli`, and each configured `[python].uv_tools` package use separate environments under `/opt/uv/tools`.
- tool commands are linked under `/opt/uv/bin`; executable ownership collisions fail instead of replacing an existing command.

`comfy-cli` is an optional user tool and is not used to install ComfyUI, Manager, or Registry custom nodes during the image build.

cdh-controlled ordinary Python resolution and installation use `[python].index_url`. Direct PyTorch packages and target-active protected requirements from the selected ComfyUI checkout form one exact group and use only the CUDA-derived PyTorch source. Their generic transitive dependencies use the ordinary Python source. A missing direct PyTorch member does not fall back to a same-named package on the ordinary source, and the selected exact group is protected from later cdh-controlled application mutations.

## Final evidence and replay boundary

After all image mutations succeed, cdh writes the strict final-state observation `/opt/cdh/build/manifest.json`. It binds the image-configuration, canonical lock, and BuildPlan digests and records intended and observed direct identities. The manifest is evidence, not another resolver, lock, replay input, support verdict, or general service-health check.

cdh provides bounded verified replay of cdh-controlled direct inputs. This does not promise an offline or byte-identical build, a complete lock of transitive dependencies or every fetched artifact, authenticity for downloads without a user-supplied checksum, deterministic effects from trusted installers or hooks, or replay of deployment-time mutations.
