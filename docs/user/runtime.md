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

Runtime configuration covers ComfyUI `listen`, `port`, and `extra_args`; cdh download settings and downloader credentials; runtime Secret sources; `system.ssh`; and `files`. Known host-only fields in a runtime TOML file are ignored with a warning. Unknown or otherwise unsupported runtime fields fail startup instead of being silently accepted. A mounted runtime file cannot install packages, change the selected ComfyUI checkout, or rebuild the image.

Each TOML source is first parsed and checked for runtime applicability. The remaining supported values are then merged with the defaults and environment overrides, and cdh validates the resulting effective runtime document. Consequently, a later partial item can inherit omitted fields from an earlier layer, but an invalid effective result still fails startup with source context.

Ordinary runtime arrays use whole-list replacement: omission inherits the earlier list, a later non-empty list replaces it, and a later empty list clears it. This applies to `comfyui.extra_args` and TOML `system.ssh.pub_keys`. `SSH_PUB_KEY` is the deliberate append exception. After the other layers are merged, cdh stably deduplicates the effective public keys by declared key type plus base64 key blob and retains the first normalized complete line and its optional comment. An `SSH_PUB_KEY` with an existing key identity is therefore a quiet no-op even when its comment differs; otherwise cdh appends its normalized line.

Downloader credential routes instead merge by canonical `match`: a later equivalent route atomically replaces the complete earlier route, a new route appends, and `credentials = []` clears the catalog. Each `[secrets.<name>]` source is an independent atomic definition. Runtime routes and sources are deployment-owned and are never inherited from their build-time counterparts.

The supported environment overrides are:

- `CDH_COMFYUI_LISTEN`, `CDH_COMFYUI_PORT`, and `CDH_COMFYUI_EXTRA_ARGS`;
- `CDH_DEFAULT_DOWNLOADER`, `CDH_DEFAULT_DOWNLOAD_MODE`, `CDH_DOWNLOAD_MAX_ATTEMPTS`, `CDH_DOWNLOAD_FAILURE_POLICY`, and `CDH_SHUTDOWN_TIMEOUT`; and
- `SSH_ENABLE`, `SSH_PORT`, `SSH_PASSWORD`, and `SSH_PUB_KEY`.

`CDH_COMFYUI_EXTRA_ARGS` uses POSIX shell-style word parsing without executing a shell. Neither runtime TOML nor the environment may place `--listen`, `--port`, `--auto-launch`, or `--disable-auto-launch` in `extra_args`; cdh owns those container launch controls.

Environment overrides and mounted runtime inputs are deployment-time changes. They are outside the baked image's verified replay boundary.

## Runtime control

Run the following commands against the container you want to control:

```bash
docker exec CONTAINER cdh container runtime restart
docker exec CONTAINER cdh container runtime status
docker exec CONTAINER cdh container runtime status --json
docker exec CONTAINER cdh container runtime follow
```

In an SSH session, use the installed absolute path `/opt/uv/bin/cdh` in place of `cdh`; the SSH login environment does not guarantee that the image entrypoint's tool path is present.

`restart` waits while cdh stops the current ComfyUI runtime and starts it again. Once accepted, the restart rereads baked and mounted runtime configuration and hooks, then runs the normal startup sequence below. The restarted runtime continues to use the container's startup environment; environment values supplied only to the `docker exec` command do not become runtime overrides. Only one restart can run at a time, so a concurrent request exits with a busy error.

A restart succeeds after ComfyUI is spawned when there are no post-start hooks. When post-start hooks exist, it succeeds only after conditional readiness and all post-start hooks complete. Asynchronous downloads need only be accepted into their queue; restart does not wait for every asynchronous transfer to finish.

Interrupting a restart before cdh accepts it cancels that request. After acceptance, interruption stops only the local wait; the restart continues in the container, and `status` shows its current state. When the client knows the accepted operation ID, `Ctrl-C` reports it. A restart failure is reported to the waiting client and makes the container exit nonzero after cleanup. cdh does not provide runtime `start` or `stop` commands, and `restart` has no detached or no-wait mode. Natural ComfyUI exit still ends the container.

`status` shows the current ComfyUI runtime and any restart in progress; `--json` emits the stable machine-readable status. This is current in-memory state, not a health check or persistent history.

`follow` streams stdout and stderr produced after connection and stays attached across a manual restart. It does not replay or persist older output; use Docker logs or the deployment logging backend for history. Stopping the command or a connection that cannot keep up affects only that live log session and never stops or slows ComfyUI.

Run these commands with the container's default user. A different UID, including one selected with `docker exec --user`, cannot access runtime control.

## Files, downloads, and persistent state

Host HTTP `[[files]]` declarations become baked runtime defaults; host-local build files do not. Runtime accepts only `type = "http"` items and rejects a mounted local source instead of trying to interpret a host path inside the container. On each container start or accepted restart, baked and mounted file lists merge by normalized `target_dir` plus `filename`. Redundant `/`, `.` segments, and a trailing `/` are canonicalized before identity comparison; `.` and `./` select the `COMFYUI_PATH` root itself. A later item for an existing target patches that item at its original position, retaining fields it omits; a new target appends. A later `files = []` clears the earlier list. The effective item must contain its HTTP type and URL, and duplicate or invalid effective targets fail after merging. Every target is relative to `COMFYUI_PATH`, and absolute paths or any authored `..` segment remain invalid.

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
url = "https://huggingface.co/acme/private-model/resolve/main/model.safetensors"
target_dir = "models/checkpoints"
filename = "model.safetensors"
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

cdh starts sshd in the foreground and owns its startup, monitoring, and shutdown. If sshd exits unexpectedly after ComfyUI starts, cdh warns but does not stop ComfyUI. The configured SSH port is the port inside the container; Docker or the deployment platform owns host port publication and network exposure.

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
