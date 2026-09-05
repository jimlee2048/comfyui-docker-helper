# Runtime and lifecycle

English | [简体中文](runtime.zh-CN.md)

This guide is for people running an image built by cdh. It explains which settings can change without rebuilding the image, how to control the running ComfyUI lifecycle from inside the container, when runtime downloads and hooks run, how optional SSH access is activated, and what happens when the container stops.

For the complete annotated host configuration, see [`examples/full.toml`](../../examples/full.toml). The [configuration guide](configuration.md) explains host configuration and layering; the [build and lock guide](build-and-lock.md) explains how host choices become baked image inputs.

## GPU host requirements

Running a cdh-built image with GPU access requires NVIDIA Container Toolkit support, an NVIDIA driver `>=580.65.06`, and a Turing-or-newer NVIDIA GPU.

## Set the container timezone

Set a standard process timezone when starting the container:

```bash
docker run --env TZ=Asia/Shanghai IMAGE
```

The container-start `TZ` overrides any value baked through `[system.env]` for programs that honor standard `TZ` behavior.

## Runtime configuration precedence

Each image contains generated runtime defaults at `/opt/cdh/runtime/config.toml`. You can mount an optional `/etc/cdh/runtime/config.toml` to change runtime-only behavior without rebuilding the image.

cdh applies runtime settings in this order, with later sources taking precedence:

```text
built-in defaults < baked config < mounted config < environment
```

Runtime configuration covers ComfyUI `listen`, `port`, and `extra_args`; cdh download and log recording settings and downloader credentials; runtime Secret sources; `system.ssh`; and `files`. Known host-only fields in a runtime TOML file are ignored with a warning. Unknown or otherwise unsupported runtime fields fail startup instead of being silently accepted. A mounted runtime file cannot install packages, change the selected ComfyUI checkout, or rebuild the image.

Each TOML source is first parsed and checked for runtime applicability. The remaining supported values are then merged with the defaults and environment overrides, and cdh validates the resulting effective runtime document. Consequently, a later partial item can inherit omitted fields from an earlier layer, but an invalid effective result still fails startup with source context.

Ordinary runtime arrays use whole-list replacement: omission inherits the earlier list, a later non-empty list replaces it, and a later empty list clears it. This applies to `comfyui.extra_args` and TOML `system.ssh.pub_keys`. `SSH_PUB_KEY` is the deliberate append exception. After the other layers are merged, cdh stably deduplicates the effective public keys by declared key type plus base64 key blob and retains the first normalized complete line and its optional comment. An `SSH_PUB_KEY` with an existing key identity is therefore a quiet no-op even when its comment differs; otherwise cdh appends its normalized line.

Downloader credential routes instead merge by canonical `match`: a later equivalent route atomically replaces the complete earlier route, a new route appends, and `credentials = []` clears the catalog. Each `[secrets.<name>]` source is an independent atomic definition. Runtime routes and sources are deployment-owned and are never inherited from their build-time counterparts.

Recording environment overrides are listed with `[cdh.logs]` in the [full example](../../examples/full.toml). The other runtime environment overrides are:

- `CDH_COMFYUI_LISTEN`, `CDH_COMFYUI_PORT`, and `CDH_COMFYUI_EXTRA_ARGS`;
- `CDH_DEFAULT_DOWNLOADER`, `CDH_DEFAULT_DOWNLOAD_MODE`, `CDH_DOWNLOAD_MAX_ATTEMPTS`, `CDH_DOWNLOAD_FAILURE_POLICY`, and `CDH_SHUTDOWN_TIMEOUT`; and
- `SSH_ENABLE`, `SSH_PORT`, `SSH_PASSWORD`, and `SSH_PUB_KEY`.

`CDH_COMFYUI_EXTRA_ARGS` uses POSIX shell-style word parsing without executing a shell. Neither runtime TOML nor the environment may place `--listen`, `--port`, `--auto-launch`, or `--disable-auto-launch` in `extra_args`; cdh owns those container launch controls.

Environment overrides and mounted runtime inputs are deployment-time changes. They are outside the baked image's verified replay boundary.

## Runtime output detail and streams

Runtime detail options belong to the root command and therefore precede `container`:

```bash
cdh -q container runtime COMMAND
cdh -v container runtime COMMAND
cdh -vv container runtime COMMAND
```

Normal `runtime serve` output is a durable plain stderr log, including generation lifecycle, runtime-file preparation, hooks, SSH, ComfyUI startup and readiness, and cleanup when those phases apply. It remains plain when stderr is a terminal so container logs are stable and line-oriented. `-q/--quiet` suppresses cdh-owned informational lifecycle and progress lines; `-v/--verbose` adds counts, timings, and operational context, while `-vv` adds debug detail. Quiet and verbose cannot be combined. Warnings and controlled errors remain visible under quiet.

Runtime downloads identify the configured target and its position in the batch and attempt sequence. They report transferred bytes and, when a compatible total is available, percentage, rate, and estimated time; an unknown total stays byte-based rather than inventing a percentage. Retry, stall, recovery, file-ready, and queue results appear as durable lines. When runtime output is heavily backlogged, cdh may combine repeated progress updates and warns if informational updates were omitted; transfer and SSH work continue.

ComfyUI, hook, and SSH child stdout and stderr remain raw child output. cdh does not prefix, restyle, filter, or redact those bytes. The original container stdout and stderr remain the primary logging streams. Root detail options likewise do not change the required human or JSON result of `runtime status`, the result of `runtime restart`, or the merged bytes returned by `runtime logs`. A logs client does not change the running controller's detail setting.

## Runtime control

Run the following commands against the container you want to control:

```bash
docker exec CONTAINER cdh container runtime restart
docker exec CONTAINER cdh container runtime status
docker exec CONTAINER cdh container runtime status --json
docker exec CONTAINER cdh container runtime logs --follow
```

Unless the deployment overrides `PATH`, an SSH session uses the image's normal tool path, so invoke `cdh` and `uv` by name. A deployment that overrides `PATH` must retain `/opt/uv/bin` to keep cdh, uv, and configured uv tools available by name.

`restart` waits while cdh stops the current ComfyUI runtime and starts it again. Once accepted, the restart rereads baked and mounted runtime configuration and hooks, then runs the normal startup sequence below. The restarted runtime continues to use the container's startup environment; environment values supplied only to the `docker exec` command do not become runtime overrides. Only one restart can run at a time, so a concurrent request exits with a busy error.

A restart succeeds after ComfyUI is spawned when there are no post-start hooks. When post-start hooks exist, it succeeds only after conditional readiness and all post-start hooks complete. Asynchronous downloads need only be accepted into their queue; restart does not wait for every asynchronous transfer to finish.

Interrupting a restart before cdh accepts it cancels that request. After acceptance, interruption stops only the local wait; the restart continues in the container, and `status` shows its current state. When the client knows the accepted operation ID, `Ctrl-C` reports it. A restart failure is reported to the waiting client and makes the container exit nonzero after cleanup. cdh does not provide runtime `start` or `stop` commands, and `restart` has no detached or no-wait mode. Natural ComfyUI exit still ends the container.

`status` shows the current ComfyUI runtime and any restart in progress; `--json` emits the stable machine-readable status. This is current in-memory state, not a health check or persistent history.

`logs` reads retained output and can keep following across a manual restart. See [Read and retain logs](#read-and-retain-logs) for examples and retention limits. The controller must be running; a missing control endpoint does not start a local runtime or switch the client to reading files.

Run these commands with the container's default user. A different UID, including one selected with `docker exec --user`, cannot access runtime control.

## Read and retain logs

Inside the container, use `cdh container runtime logs`; from the host, prefix the same command with `docker exec CONTAINER`. The command returns captured cdh lifecycle output and ComfyUI, hook, or service output inherited through the runtime's stdout/stderr. It does not collect arbitrary log files, Docker build output, or separate exec/SSH command sessions.

```bash
# Read all retained output and exit.
cdh container runtime logs

# Read the last 200 lines, or read them and continue following.
cdh container runtime logs --tail 200
cdh container runtime logs --tail 200 --follow

# Follow only new output.
cdh container runtime logs --tail 0 --follow

# Export merged output; diagnostics remain on the terminal's stderr.
cdh container runtime logs > runtime.log
```

`--tail` also accepts `all`; `-n` and `-f` are the short forms of `--tail` and `--follow`. The old `runtime follow` command is replaced by `runtime logs --tail 0 --follow`. Using `logs --follow` without a tail limit first returns all retained history.

Query payload merges both source streams into stdout in cdh's observed order. Warnings and errors go to stderr; the original container stdout/stderr remain separate and available to Docker's logging backend. cdh preserves payload bytes, including ANSI, carriage returns, invalid UTF-8, and unfinished lines. Tail counts LF-delimited lines and a final nonempty fragment; it does not treat a carriage return as a newline. Directly readable `runtime.log` and numbered archives contain these same raw bytes, without JSON envelopes, added timestamps, or stream labels. Rotation may split a line between files, and appending after restart may continue a previous unfinished line.

Choose recording behavior with `[cdh.logs]` in image configuration or mounted runtime TOML. The [full example](../../examples/full.toml) documents the exact settings, size units, and container-start environment overrides.

| Mode | Available history |
| --- | --- |
| `file` (default) | Retained rotating files plus the current memory tail, without duplicate bytes. |
| `memory` | This controller's bounded recent output; previous files are not read. |
| `none` | No new history. Queries can read existing owned files without creating, rotating, or deleting them, with a recording-disabled warning. An absent store is empty success. |

Live-only viewing works in every mode. With `none`, history followed by live output warns that the deliberately unrecorded interval cannot be recovered. Disabling recording does not disable primary container output or delete old history.

The default directory is `/var/log/cdh`. `max_size` independently bounds the memory payload and each file, and `max_files` includes the active file. The defaults permit 20 MiB in memory plus up to 100 MiB in retained files. Memory grows on demand. Queue, reader, and metadata allocations are additional bounded costs, so these payload limits are not a total process-memory limit. Internal resource limits can also interrupt recording or a query; increasing a retention setting does not remove those limits.

Logging settings are fixed for the controller's lifetime. Editing valid settings and running `runtime restart` produces a warning and keeps the original recording settings until the container restarts. Invalid effective logging values still fail restart admission and can make the container exit. Equivalent size spellings do not count as changes. A full container restart uses the newly selected directory without moving or deleting the old directory's logs. Environment values passed only to a logs client do not select storage.

### Persist files and prepare the directory

Memory survives a manual ComfyUI restart, but is lost when the controller or container restarts. Written files survive an ordinary restart of the same container while its filesystem remains available. To retain them across container deletion/recreation, use a retained volume or bind mount; tmpfs is not persistent storage.

Use a dedicated canonical absolute container directory. cdh creates it with mode `0700` and its files with mode `0600`; existing owned artifacts must have those modes and belong to the runtime's effective UID. Directory ancestors must be real directories owned by root or that UID and must not be unsafely writable. Symlinks, unexpected hardlinks, unsafe artifacts, and a second writer are refused. cdh does not recursively change mount permissions or ownership.

Mount a parent and let cdh create its private child. For example, with the image's default user:

```bash
docker run --gpus all --name comfyui \
  --mount type=volume,source=cdh-logs,target=/logs \
  --env CDH_LOG_DIRECTORY=/logs/cdh \
  IMAGE
```

An existing volume root with mode `0755` is suitable as that parent, but is not itself an admitted `0700` log directory. Directly selecting such a root makes file recording fall back to memory. Stop the old container before reusing its log directory; concurrently running replicas need separate directories. Read files with ordinary tools if useful, but leave cdh's filenames, lock marker, and rotation contents unchanged while it manages them.

### Failures, gaps, and command results

If directory admission, writing, rotation, sync, or the bounded file queue fails, cdh warns and stops file recording for the rest of that controller run. Memory and live output continue, and safe existing files remain readable. Fix the storage issue and restart the container to retry file recording; cdh does not automatically recover or copy missed memory output back into files.

A persistence warning alone does not make a complete query fail. If retained files end before the available memory tail begins, all-history returns both available portions, reports the gap on stderr, and exits nonzero. A recent tail fully covered by the continuous memory suffix can still succeed. Normal eviction of the oldest retained prefix is a retention limit, not an error. A known gap, read failure, or eviction during a query is reported rather than silently presented as complete history. Incomplete initial history ends `--follow` with a nonzero result instead of silently switching to live output.

A slow client can be disconnected without stopping ComfyUI or backpressuring its primary output. `Ctrl-C` ends only the client and returns 130. A successful finite read requires the controller's explicit completion result. Following continues across ComfyUI restart and succeeds on an explicit live-end result during ordinary controller shutdown; unexpected EOF, lost delivery, or exhausted shutdown delivery budget is nonzero. Successful following covers output through the controller's final delivery cutoff; it does not guarantee later shutdown output or persistence to disk. There is no automatic reconnection after container restart.

On normal shutdown, cdh drains and syncs within the existing shutdown budget. Forced termination, an exhausted budget, or host/storage failure can lose queued or unsynced final output. Raw files cannot reconstruct unknown losses from a previous controller run, so retained history is not a complete audit trail. Initial runtime errors are recorded when logging could be admitted; malformed TOML, invalid log settings, and failures before capture can prevent that. Use Docker logs or the deployment backend for those earlier diagnostics and for its independent retention.

## Files, downloads, and persistent state

Host HTTP `[[files]]` declarations become baked runtime defaults; host-local build files do not. Runtime accepts only `type = "http"` items and rejects a mounted local source instead of trying to interpret a host path inside the container. Each HTTP item uses `source` for its host-qualified URL and `target` for its exact final file path relative to `COMFYUI_PATH`; runtime does not accept a directory target, a root target, or a target filename inferred from the URL. Redundant `/` and `.` segments normalize before identity comparison, while empty values, absolute paths, explicit `..`, backslashes, controls, and trailing slashes are invalid. A later item for an existing target patches that item at its original position, retaining fields it omits; a new target appends. A later `files = []` clears the earlier list. Duplicate or overlapping effective targets fail after merging, and a mounted runtime file list cannot contain local items.

Runtime targets follow the same [file naming rules](configuration.md#add-files-during-the-image-build) as build targets: no complete destination component may be `.cdh-staging` or start with `.wh.`.

Synchronous downloads finish before pre-start hooks. Asynchronous downloads are accepted into one background queue before ComfyUI starts and may continue while it runs; they do not gate ComfyUI readiness.

`download_max_attempts` is the total number of backend invocations allowed for each file during one container start or accepted restart, including the first attempt. `download_failure_policy` applies only at runtime:

- for synchronous files, `fail` aborts startup after an ordinary terminal failure or exhausted attempt budget, while `continue` moves to later files;
- for asynchronous files, `fail` stops the remaining queue without stopping ComfyUI, while `continue` moves to later queued files; and
- containment, unsafe target type, permission, identity, persistence, and durability failures always fail closed and are not converted into `continue`.

Build-time files have a different contract: every declared build file is required and authoritatively replaces lower-image content. The `overwrite` setting below is runtime-only. See the [build and lock guide](build-and-lock.md#build-files-and-local-context-materialization).

An optional `checksum = "sha256:<64 hexadecimal digits>"` declares trusted content identity. Obtain the digest from a source independent enough for your threat model; cdh does not fetch or infer it from the download origin.

| Existing target | `overwrite` | Result |
| --- | --- | --- |
| Matches the configured checksum | Either value | Keep the verified file |
| Does not match the configured checksum | `false` | Keep the existing file and fail |
| Does not match the configured checksum | `true` | Replace only after the complete new file passes verification |
| No checksum is configured | `false` | Keep the existing regular file as unverified |
| No checksum is configured | `true` | Replace atomically after completed transport, without claiming content authenticity |

cdh keeps an existing final file unchanged until a complete replacement is ready for atomic publication. Without a checksum, successful transport and atomic replacement are not proof that the downloaded bytes are authentic.

If an operation fails after replacing a target, the complete new file may already be present; cdh does not restore the old file. Inspect the target before retrying.

Runtime reconciliation state lives at `/var/lib/cdh/runtime/state.json`. This file is cdh-owned internal recovery state, not user configuration or a download-history API. Do not edit it. Runtime downloads require the state location to be writable. Mount `/var/lib/cdh/runtime` to preserve recovery state across container replacement, and mount each target directory that must preserve downloaded files. Preserving the state file alone does not preserve the downloaded files.

Stop the old cdh container before starting its replacement with the same persisted state and download targets. Do not let overlapping instances or replicas write the same state file or the same download targets; separate state files do not make a shared target safe.

## Authenticated HTTPX downloads

Build-time downloader routes and Secret definitions are never baked into runtime configuration. To authenticate a runtime download, declare an independent route and container-visible Secret source in the mounted `/etc/cdh/runtime/config.toml`:

```toml
[secrets.hf_read]
file = "/run/secrets/hf_read"

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

A runtime Secret selects exactly one `env` or `file` source. Environment locators name variables already present in the container's startup environment. File locators must be absolute container paths. cdh follows deployment-managed symlink projections such as Kubernetes Secret mounts, then requires the resolved object to be a regular file and reads at most 65,525 bytes from one opened descriptor. It does not warn about projected-file modes such as `0444` or `0644`; the deployment owns mount mode, ACL, namespace, and same-UID access.

Each runtime generation validates route structure and references without reading Secret content. After file reconciliation, the generation reads a selected Secret only immediately before its first protected outbound request and caches that value in memory for the rest of the generation. A completed target that schedules no network request does not require its Secret. An accepted runtime restart discards the previous snapshot and resolves each needed source again, so replacing a projected file and restarting rotates its value; changing the process environment normally requires recreating the container.

Missing, unreadable, or invalid Bearer content fails locally without retrying that credential failure. Before an initial protected request it consumes zero network attempts; if a public request redirects into a protected route, the already completed attempt remains visible. The effective `download_failure_policy` then applies with its existing synchronous or asynchronous queue behavior. Real HTTP responses such as 401 or 403 remain ordinary download failures.

Route definitions, Secret references and locators, resolved values, and value hashes do not enter runtime desired-content identity or persisted download state. Rotating a credential therefore does not redownload a completed file; pending work in a new generation uses that generation's value. cdh does not write the token or generated Authorization value to state, status, history, manifest, or its own logs. Code running as the same container UID remains within the deployment trust boundary and is not sandboxed from a Secret that the deployment makes readable.

## SSH and confidential values

SSH provides opt-in root access and is disabled by default. Enable it in runtime TOML or with `SSH_ENABLE=true`, and provide at least one valid public key or password. Credentials do not enable SSH by themselves. If SSH is enabled without an effective credential, cdh warns, does not start sshd, and continues normal ComfyUI startup.

Prefer `SSH_PUB_KEY` or `SSH_PASSWORD` at container startup instead of baking credentials into the image. `SSH_PUB_KEY` appends one normalized public key to the configured key set. When SSH is enabled, the container generates its own host keys during startup; cdh-built images do not share package-generated host keys.

Runtime public keys use the same plain-line syntax and supported security-key algorithms described in the [configuration guide](configuration.md#layer-configuration). An `authorized_keys` options prefix is not accepted.

An authenticated SSH session inherits the effective environment present when the container starts, including image `ENV`, `[system.env]`, and values added or overridden by Docker or another OCI runtime. OpenSSH supplies `TERM` and `SSH_AUTH_SOCK` for the current connection; cdh does not replace those two values with their container-start versions. Other variables are not filtered based on their names or contents.

An authenticated root session can therefore inspect inherited passwords, tokens, and Secret-source environment values. Image builders and deployment operators are responsible for every value they place in that environment; cdh SSH access is not a Secret-isolation boundary and does not redact output produced by sshd, shell profiles, or commands run after authentication.

If SSH is enabled with credentials but cdh cannot preserve the environment or prepare and start sshd, container startup fails instead of providing a partial SSH service. If sshd exits unexpectedly after ComfyUI has started, cdh warns but does not stop ComfyUI. The configured SSH port is inside the container; Docker or the deployment platform owns host port publication and network exposure.

An SSH-associated login shell that loads the image's system profile automatically enters the effective `WORKSPACE`. This applies to the default interactive `ssh root@host` login and to explicitly requested login-shell commands that load the system profile, such as `ssh root@host 'bash -lc "pwd"'` or `ssh root@host 'sh -lc "pwd"'`. Shell startup modes that do not load the system profile do not receive this convenience. An ordinary remote command such as `ssh root@host pwd` does not invoke a login shell and starts in `/root`. If a consuming login shell cannot enter `WORKSPACE`, it changes to `/root`, prints `Warning: cdh could not enter WORKSPACE; continuing in /root`, and continues there.

Images built by current cdh place cdh, uv, and configured uv tools on the image's default tool path. Rebuild images created before this behavior was available; changing runtime configuration alone cannot update existing image contents.

When cdh creates `/root/.ssh` and `authorized_keys`, it uses modes `0700` and `0600`. An existing root-owned `.ssh` directory is admitted when it is not writable by group or other; a safe non-`0700` mode is preserved with a warning. The directory must still allow the temporary-file and atomic replacement operations that cdh attempts. Read-only mounts, access-control or capability restrictions, and other I/O failures remain fatal. An existing root-owned regular `authorized_keys` file is eligible for replacement when it is not writable by group or other; a safe non-`0600` mode warns, and the atomically replaced file is still `0600`. Wrong ownership, writable group/other bits, symlinks, and special files also remain fatal.

Atomic replacement changes the `authorized_keys` inode. A deployment that directly bind-mounts that file may reject replacement; mount the parent `.ssh` directory with a safe mode or supply keys through runtime configuration instead. cdh does not fall back to an in-place credential write.

Root SSH expands the container's attack surface. Protect configuration, environment values, rendered contexts, image artifacts, registries, logs, and runtime access accordingly. cdh avoids printing the explicit SSH password and keeps its own temporary credentials internal, but it does not guess that arbitrary TOML values, URLs, arguments, or environment variables are secrets.

## Runtime hooks and startup readiness

Pass `--runtime-hooks-dir <dir>` to `cdh host render` or `cdh host build` to bake a runtime hook tree. Omitting the option bakes no runtime hooks. See the [runtime hook examples](../../examples/runtime-hooks/).

The tree uses these phase directories:

```text
pre-start.d/
post-start.d/
stop.d/
```

Only direct regular `.sh` or `.py` files in a phase directory are selected as hooks. Ordinary files with other suffixes and ordinary directories are ignored without recursion, with concise warnings aggregated by source and phase. Symlinks, special files, inspection/read failures, and invalid known phase paths remain startup errors. Shell hooks run with `bash`; Python hooks run with the managed application Python. Hooks receive the container runtime environment and run with `COMFYUI_PATH` as their working directory.

Baked hooks are selected, content-verified image inputs under `/opt/cdh/runtime/hooks`. You can also mount deployment hooks at `/etc/cdh/runtime/hooks`; mounted hooks remain external runtime inputs and are not part of the image lock. Baked hooks run before mounted hooks, and filenames run in lexical order within each source and phase.

Both baked and mounted hooks are trusted executable code. cdh verifies the selected baked bytes, but it does not sandbox a hook or make the hook's filesystem, network, package, or process effects reproducible.

The startup order is:

```text
synchronous downloads
  -> pre-start hooks
  -> optional sshd
  -> asynchronous queue acceptance
  -> ComfyUI
  -> conditional readiness
  -> post-start hooks
```

cdh waits for readiness only when at least one post-start hook exists. It probes the effective ComfyUI port on loopback at `/system_stats` and requires an HTTP 200 JSON object with `system` and `devices`. If ComfyUI exits before readiness or the bounded readiness wait expires, startup fails and post-start hooks do not run.

This complete startup order runs at initial container startup and for each accepted restart.

This readiness gate means the ComfyUI API is serving after startup initialization. It is not a general container health check and does not prove that every custom node, workflow, model, GPU path, or production workload works.

## Background services started by hooks

cdh owns a hook while that hook's leader is running. After the leader finishes, cdh does not discover, supervise, health-check, or signal a background process that the hook deliberately left running.

If a startup hook launches a service, pair it with a stop hook that uses the service's own control interface, or a carefully validated process identity, to request termination and wait for exit. A missing or failed stop hook, natural ComfyUI exit, external `SIGKILL`, or early container teardown provides no graceful-shutdown guarantee for that service. Container teardown can terminate a remaining process, but that is not graceful service shutdown.

## Signals and shutdown

Every cdh-built image runs Tini as PID 1 with cdh as its direct child. Tini forwards the container's stop signal to cdh and reaps adopted orphan processes. It is not a service supervisor or health checker.

On the first `SIGTERM` or `SIGINT`, cdh:

1. stops admitting asynchronous work and starts cancellation of the download queue and sshd;
2. runs stop hooks in order while ComfyUI remains available;
3. forwards the original signal to ComfyUI; and
4. waits for cdh-managed processes to exit and be reaped.

`shutdown_timeout` is one total monotonic budget for stopping the current ComfyUI runtime, whether shutdown begins from an external signal or an accepted manual restart. Its default is eight seconds, with the final two seconds reserved for signaling ComfyUI and reaping managed children. When the earlier hook portion expires, cdh terminates the active hook and skips later hooks. At the total deadline it force-stops managed work that is still alive. A Docker shutdown accepted during restart takes precedence, prevents ComfyUI from starting again, and cannot extend a deadline that is already running.

A second `SIGTERM` or `SIGINT` skips the remaining grace period and enters force shutdown immediately. A force-killed ComfyUI normally makes the container exit with code 137. When ComfyUI exits naturally, cdh preserves its exit result, cleans up its auxiliary work, and does not run signal-only stop hooks.

Docker or another orchestrator owns a separate external hard limit. Docker Engine uses a 10-second default for Linux containers when no container-specific timeout is configured, and Docker Compose defaults `stop_grace_period` to 10 seconds. cdh's eight-second default leaves only a best-effort scheduling margin. Configure Docker [`--stop-timeout`](https://docs.docker.com/reference/cli/docker/container/run/#options) or Compose [`stop_grace_period`](https://docs.docker.com/reference/compose-file/services/#stop_grace_period) to be greater than the cdh total when hooks need more time.

Setting `shutdown_timeout = -1` disables only the cdh outer and hook deadlines for external shutdown and manual restart. cdh-owned component operations remain bounded, and Docker's own timeout is independent. No cleanup can continue after an external `SIGKILL`.
