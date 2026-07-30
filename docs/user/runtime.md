# Runtime and lifecycle

English | [简体中文](runtime.zh-CN.md)

This guide is for people running an image built by cdh. It explains which settings can change without rebuilding the image, when runtime downloads and hooks run, how optional SSH access is activated, and what happens when the container stops.

For the complete annotated host configuration, see [`examples/full.toml`](../../examples/full.toml). The [configuration guide](configuration.md) explains host configuration and layering; the [build and lock guide](build-and-lock.md) explains how host choices become baked image inputs.

## GPU host requirements

Running a cdh-built image with GPU access requires NVIDIA Container Toolkit support, an NVIDIA driver `>=580.65.06`, and a Turing-or-newer NVIDIA GPU.

## Runtime configuration precedence

Each image contains generated runtime defaults at `/opt/cdh/runtime/config.toml`. You can mount an optional `/etc/cdh/runtime/config.toml` to change runtime-only behavior without rebuilding the image.

cdh applies runtime settings in this order, with later sources taking precedence:

```text
built-in defaults < baked config < mounted config < environment
```

Runtime configuration covers ComfyUI `listen`, `port`, and `extra_args`; cdh download settings; `system.ssh`; and `files`. Known host-only fields in a runtime TOML file are ignored with a warning. Unknown or otherwise unsupported runtime fields fail startup instead of being silently accepted. A mounted runtime file cannot install packages, change the selected ComfyUI checkout, or rebuild the image.

The supported environment overrides are:

- `CDH_COMFYUI_LISTEN`, `CDH_COMFYUI_PORT`, and `CDH_COMFYUI_EXTRA_ARGS`;
- `CDH_DEFAULT_DOWNLOADER`, `CDH_DEFAULT_DOWNLOAD_MODE`, `CDH_DOWNLOAD_MAX_ATTEMPTS`, `CDH_DOWNLOAD_FAILURE_POLICY`, and `CDH_SHUTDOWN_TIMEOUT`; and
- `SSH_ENABLE`, `SSH_PORT`, `SSH_PASSWORD`, and `SSH_PUB_KEY`.

`CDH_COMFYUI_EXTRA_ARGS` uses POSIX shell-style word parsing without executing a shell. Neither runtime TOML nor the environment may place `--listen`, `--port`, `--auto-launch`, or `--disable-auto-launch` in `extra_args`; cdh owns those container launch controls.

Environment overrides and mounted runtime inputs are deployment-time changes. They are outside the baked image's verified replay boundary.

## Files, downloads, and persistent state

Host `[[files]]` declarations become baked runtime defaults. At container startup, baked and mounted file lists merge by normalized `dir` plus `filename`; `files = []` in a later layer clears the earlier list. Every target is relative to `COMFYUI_PATH`.

Synchronous downloads finish before pre-start hooks. Asynchronous downloads are accepted into one background queue before ComfyUI starts and may continue while it runs; they do not gate ComfyUI readiness.

`download_max_attempts` is the total number of backend invocations allowed for each file during one container start, including the first attempt. `download_failure_policy` applies only at runtime:

- for synchronous files, `fail` aborts startup after an ordinary terminal failure or exhausted attempt budget, while `continue` moves to later files;
- for asynchronous files, `fail` stops the remaining queue without stopping ComfyUI, while `continue` moves to later queued files; and
- containment, unsafe target type, permission, identity, persistence, and durability failures always fail closed and are not converted into `continue`.

Build-time files have a different contract: every declared build file is required. See the [build and lock guide](build-and-lock.md).

An optional `checksum = "sha256:<64 hexadecimal digits>"` declares trusted content identity. Obtain the digest from a source independent enough for your threat model; cdh does not fetch or infer it from the download origin.

| Existing target | `overwrite` | Result |
| --- | --- | --- |
| Matches the configured checksum | Either value | Keep the verified file |
| Does not match the configured checksum | `false` | Keep the existing file and fail |
| Does not match the configured checksum | `true` | Replace only after the complete new file passes verification |
| No checksum is configured | `false` | Keep the existing regular file as unverified |
| No checksum is configured | `true` | Replace atomically after completed transport, without claiming content authenticity |

cdh keeps an existing final file unchanged until a complete replacement is ready for atomic publication. Without a checksum, successful transport and atomic replacement are not proof that the downloaded bytes are authentic.

The atomic rename is the replacement commit point. A later durability,
verification, cleanup, or recovery-state persistence failure still stops that
operation, but the complete new file may remain at the target; cdh does not
roll it back to the old file.

Runtime reconciliation state lives at `/var/lib/cdh/runtime/state.json`. This file is cdh-owned internal recovery state, not user configuration or a download-history API. Do not edit it. Runtime downloads require the state location to be writable. Mount `/var/lib/cdh/runtime` to preserve recovery state across container replacement, and mount each target directory that must preserve downloaded files. Preserving the state file alone does not preserve the downloaded files.

Stop the old cdh container before its replacement starts using the preserved
state and download targets. Do not share the writable state, or targets and
staging files governed by it, between overlapping rolling instances or
replicas. Using different state files does not make concurrent writers safe
when they control the same download target or staging namespace.

## SSH and confidential values

SSH provides opt-in root access and is disabled by default. Enable it in runtime TOML or with `SSH_ENABLE=true`, and provide at least one valid public key or password. Credentials do not enable SSH by themselves. If SSH is enabled without an effective credential, cdh warns, does not start sshd, and continues normal ComfyUI startup.

Prefer `SSH_PUB_KEY` or `SSH_PASSWORD` at container startup instead of baking credentials into the image. `SSH_PUB_KEY` appends one normalized public key to the configured key set. When SSH is enabled, the container generates its own host keys during startup; cdh-built images do not share package-generated host keys.

cdh starts sshd in the foreground and owns its startup, monitoring, and shutdown. If sshd exits unexpectedly after ComfyUI starts, cdh warns but does not stop ComfyUI. The configured SSH port is the port inside the container; Docker or the deployment platform owns host port publication and network exposure.

Root SSH expands the container's attack surface. Protect configuration, environment values, rendered contexts, image artifacts, registries, logs, and runtime access accordingly. cdh avoids printing the explicit SSH password and keeps its own temporary credentials internal, but it does not guess that arbitrary TOML values, URLs, arguments, or environment variables are secrets.

## Runtime hooks and startup readiness

Pass `--runtime-hooks-dir <dir>` to `cdh host render` or `cdh host build` to bake a runtime hook tree. Omitting the option bakes no runtime hooks. See the [runtime hook examples](../../examples/runtime-hooks/).

The tree uses these phase directories:

```text
pre-start.d/
post-start.d/
stop.d/
```

Hook entries in a phase directory must be regular `.sh` or `.py` files. Shell hooks run with `bash`; Python hooks run with the managed application Python. Hooks receive the container runtime environment and run with `COMFYUI_PATH` as their working directory.

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

`shutdown_timeout` is one total monotonic budget for this signal path. Its default is eight seconds, with the final two seconds reserved for signaling ComfyUI and reaping managed children. When the earlier hook portion expires, cdh terminates the active hook and skips later hooks. At the total deadline it force-stops managed work that is still alive.

A second `SIGTERM` or `SIGINT` skips the remaining grace period and enters force shutdown immediately. A force-killed ComfyUI normally makes the container exit with code 137. When ComfyUI exits naturally, cdh preserves its exit result, cleans up its auxiliary work, and does not run signal-only stop hooks.

Docker or another orchestrator owns a separate external hard limit. Docker Engine uses a 10-second default for Linux containers when no container-specific timeout is configured, and Docker Compose defaults `stop_grace_period` to 10 seconds. cdh's eight-second default leaves only a best-effort scheduling margin. Configure Docker [`--stop-timeout`](https://docs.docker.com/reference/cli/docker/container/run/#options) or Compose [`stop_grace_period`](https://docs.docker.com/reference/compose-file/services/#stop_grace_period) to be greater than the cdh total when hooks need more time.

Setting `shutdown_timeout = -1` disables only the cdh outer and hook deadlines. cdh-owned component operations remain bounded, and Docker's own timeout is independent. No cleanup can continue after an external `SIGKILL`.
