# Run and operate ComfyUI

English | [简体中文](runtime.zh-CN.md)

This guide is for operators running a cdh-built image. Start with a local image such as `my-comfy:dev` from the [build guide](build-and-lock.md), then add deployment settings as needed.

## GPU host requirements

GPU execution requires NVIDIA Container Toolkit support, an NVIDIA driver `>=580.65.06`, and a Turing-or-newer NVIDIA GPU. The image targets Linux `amd64`. Being able to build on Windows or macOS does not establish GPU runtime support on that host.

## Start and stop ComfyUI

On a GPU-capable Docker host with `my-comfy:dev` loaded, start a named container and publish the default ComfyUI port to local clients:

```bash
docker run -d --name comfyui --gpus all \
  -p 127.0.0.1:8188:8188 \
  my-comfy:dev
docker logs --follow comfyui
```

Open [http://localhost:8188](http://localhost:8188) on that host after startup. The page loading confirms web access; run a workflow with your models to check the workload you intend to use. `runtime status` reports process state, not GPU or workflow health. If the page is unavailable, inspect Docker logs for startup errors and confirm the port mapping. The loopback mapping above does not expose the UI to other machines.

`Ctrl-C` stops the log viewer, not the detached container. Stop and later start the same container with:

```bash
docker stop comfyui
docker start comfyui
```

After stopping it, `docker rm comfyui` removes the container and its writable layer. Preserve required data with mounts before replacing it; see [persistence](#files-downloads-and-persistent-state). The name must be available before repeating `docker run`.

## Change runtime settings

The image supplies defaults. Mount a runtime configuration directory to override supported settings without rebuilding. For example, save this as `runtime/config.toml` under your current directory:

```toml
[comfyui]
port = 8188
extra_args = []

[cdh]
shutdown_timeout = 20
```

In a POSIX shell, start a new container with that directory mounted:

```bash
docker run -d --name comfyui --gpus all \
  -p 127.0.0.1:8188:8188 \
  --mount "type=bind,source=$(pwd)/runtime,target=/etc/cdh/runtime,readonly" \
  --stop-timeout 30 \
  --env TZ=Asia/Shanghai \
  my-comfy:dev
```

Use your shell's absolute-path syntax on other hosts. Mounting the directory lets cdh see a replacement `config.toml` when you restart the application. Stop/remove an existing container with the same name before creating this one, after preserving its data.

Precedence is **built-in defaults → image defaults → mounted configuration → container-start environment**. Later sources override earlier ones. Known build-only fields in runtime TOML warn and are ignored; unknown fields or an invalid effective configuration fail startup.

| Change | How to apply it |
| --- | --- |
| Mounted ComfyUI, download, SSH settings or hooks | `cdh container runtime restart` inside the running container |
| A file-backed download token | Replace the file and restart the application; pending requests read the new value |
| Valid log recording settings | Restart the whole container; application restart retains the current recording settings |
| Container environment, port publication or mounts | Recreate the container with the new Docker options |
| Installed packages, ComfyUI version, baked files or hooks | Rebuild the image, then recreate the container |

Mounted tables inherit omitted fields. Ordinary lists replace the earlier list; `[]` clears it. Files merge by target, credentials replace by matching route, and named secret sources replace as complete definitions. `SSH_PUB_KEY` is an exception: it appends one key after configuration merging, ignoring a duplicate key identity.

Use structured `comfyui.listen` and `comfyui.port`, or `CDH_COMFYUI_LISTEN` and `CDH_COMFYUI_PORT`, for those settings. Do not put `--listen`, `--port`, `--auto-launch` or `--disable-auto-launch` in `extra_args`. `CDH_COMFYUI_EXTRA_ARGS` uses shell-style word parsing without running a shell. `TZ` overrides the image timezone for programs that honor it. For other container-start overrides:

| Setting | Environment variable |
| --- | --- |
| Default downloader and mode | `CDH_DEFAULT_DOWNLOADER`, `CDH_DEFAULT_DOWNLOAD_MODE` |
| Attempt limit and failure policy | `CDH_DOWNLOAD_MAX_ATTEMPTS`, `CDH_DOWNLOAD_FAILURE_POLICY` |
| Shutdown budget | `CDH_SHUTDOWN_TIMEOUT` |
| SSH access | `SSH_ENABLE`, `SSH_PORT`, `SSH_PASSWORD`, `SSH_PUB_KEY` |

The [full example](../../examples/full.toml) documents setting values and the `CDH_LOG_*` recording overrides.

## Restart and inspect the application

```bash
docker exec comfyui cdh container runtime restart
docker exec comfyui cdh container runtime status
docker exec comfyui cdh container runtime status --json
```

Restart stops the current application and auxiliary work, rereads configuration and hooks, and runs the startup sequence again. It uses the original container environment; variables supplied only to `docker exec` do not become runtime settings. Run control commands as the container's default user; another UID cannot access the controller. Deployments overriding `PATH` must retain `/opt/uv/bin`.

| Situation | Outcome |
| --- | --- |
| Normal restart | Waits until ComfyUI is spawned; if post-start hooks exist, also waits for readiness and those hooks |
| Asynchronous downloads | Queue acceptance is enough; restart does not wait for all transfers |
| Another restart is running | Request fails busy |
| `Ctrl-C` before acceptance | Cancels the request |
| `Ctrl-C` after acceptance | Ends the local wait; the operation continues. Check `status` |
| Restart fails | Reports failure, cleans up and exits the container nonzero |

There is no cdh runtime `start` or `stop` command: use Docker to manage the container. A missing controller is an error, not a request to start one locally. Natural ComfyUI exit ends the container. Status is current state, not persistent history or a health check.

## Read and retain logs

Docker logs are the first place to inspect startup errors. cdh also records bounded application, hook, service and lifecycle output for queries inside the container:

```bash
docker exec comfyui cdh container runtime logs --tail 200
docker exec comfyui cdh container runtime logs --tail 200 --follow
docker exec comfyui cdh container runtime logs --tail 0 --follow
docker exec comfyui cdh container runtime logs > runtime.log
```

Without a tail limit, queries return all retained history. `--tail all` does the same; `-n` and `-f` are short forms. `--tail 0 --follow` watches only new output. Following survives application restart, but reconnect after a container restart. `Ctrl-C` ends only the client and returns 130.

Queries merge captured stdout/stderr into stdout; diagnostics remain on stderr. They do not collect arbitrary log files or separate exec/SSH sessions. ComfyUI and child output is not redacted or filtered by cdh. For cdh detail options, place `-q`, `-v` or `-vv` before `container`; these do not change the running controller's detail when used on a logs client.

Configure `[cdh.logs]` in image or mounted TOML:

| Mode | Available history |
| --- | --- |
| `file` (default) | Rotating files and recent memory output, without duplicated bytes |
| `memory` | This controller's bounded memory only; old files are not read |
| `none` | No new history; existing owned files can still be read with a warning |

Live-only viewing works in every mode, and recording choices do not disable Docker output. In `none`, history followed by live output warns about the unrecorded interval. No stored files is an empty successful query.

The [full example](../../examples/full.toml) owns exact settings and size syntax. Defaults allow 20 MiB in memory plus 100 MiB across files. `max_size` limits memory and each file separately; `max_files` includes the active file. Other process allocations add overhead.

### Persist log files

Memory survives application restart, not container restart. Written files survive restarting the same container; use a volume or bind mount to retain them when replacing it. tmpfs does not persist.

Mount a parent directory and let cdh create a private child. For a new container, add:

```text
--mount type=volume,source=cdh-logs,target=/logs
--env CDH_LOG_DIRECTORY=/logs/cdh
```

The dedicated log directory must belong to the runtime user with mode `0700`; its files use `0600`. Ancestors must be real, safely owned directories, and links or unsafe artifacts are refused. A typical `0755` volume root is suitable as the parent, not the private child. cdh does not repair mount permissions recursively. Stop the old container before sharing its log directory with its replacement; concurrent replicas need separate directories. Do not modify cdh's log files or rotation markers while it manages them.

### Handle recording and query failures

| Symptom | Action or limit |
| --- | --- |
| File recording fails | cdh warns and keeps new output in memory; fix storage and restart the container to retry. Missed file output is not backfilled |
| Query reports a gap or read failure | Available output may still be returned, but the command fails; incomplete history does not enter follow mode |
| Old output was removed by normal retention | This is not an error; use larger retention or external logging if needed |
| Slow follower is disconnected | Reconnect; it does not delay primary output or stop ComfyUI |
| Early startup error | Check Docker logs; cdh recording may not yet exist |

A storage warning alone does not fail an otherwise complete query. A recent tail can succeed even when older history has a gap. Unexpected disconnection fails the client. Forced shutdown, time limits and storage failure can lose final output; retained logs are not a complete audit trail. Details for implementers live in the [log contract](../dev/contracts.md#retained-logs-are-bounded-optional-copies).

## Files, downloads, and persistent state

HTTP build-file declarations become runtime defaults; local build sources do not. Runtime accepts only HTTP files and uses the [same target naming rules](configuration.md#add-files-during-the-image-build). A later entry for the same target patches it, a new target appends, and `files = []` clears the list. Effective targets cannot overlap.

| Download mode | Startup behavior |
| --- | --- |
| Synchronous | Completes before pre-start hooks |
| Asynchronous | Enters a background queue before ComfyUI starts; does not gate readiness |

`download_max_attempts` includes the first attempt. After ordinary failures exhaust that budget, `download_failure_policy = "fail"` aborts startup for synchronous work or stops the remaining asynchronous queue without stopping ComfyUI. `continue` advances to later files. Unsafe paths, permissions, identity or persistence failures remain fatal regardless of that setting. Build-time downloads are always required.

Supply a trusted `sha256:` checksum when content identity matters; cdh does not obtain one independently from the origin.

| Existing target | `overwrite` | Result |
| --- | --- | --- |
| Matches checksum | Either | Keep verified file |
| Differs from checksum | `false` | Keep existing file and fail |
| Differs from checksum | `true` | Replace after new content verifies |
| No checksum supplied | `false` | Keep existing regular file as unverified |
| No checksum supplied | `true` | Replace after complete transport, without an authenticity claim |

The old file stays in place until the replacement is ready. If a later operation fails after replacement, the new file may already be present; inspect it before retrying. Without a checksum, successful download does not prove authenticity.

To preserve downloads across container replacement, mount their target directories **and** `/var/lib/cdh/runtime` for recovery state. For the default ComfyUI path, model persistence can use a named volume at `/workspace/ComfyUI/models`. Persisting state alone does not preserve files. Do not edit `state.json` or let overlapping containers write the same state or targets; stop the old owner first.

## Authenticated HTTPX downloads

Build credentials are not inherited. Put an independent source, route and file in mounted runtime TOML:

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

Make the token available at that absolute container path or select an `env` source already present at container startup. For Docker file rotation, bind-mount the containing directory read-only at `/run/secrets` so replacement files remain visible. Use the [token format and routing rules](configuration.md#authenticate-httpx-file-downloads). Deployment-managed symlink projections, such as Kubernetes Secrets, are supported; the resolved file must be regular. The deployment owns permissions and access by other code running as the same user.

To rotate a file-backed token:

1. Replace the mounted source value.
2. Run `docker exec comfyui cdh container runtime restart`.
3. Check logs for pending downloads that need the new value.

A value is read when first needed for a protected request and reused until application restart. Completed files that need no request need no token and are not redownloaded merely because it changed. Changing environment-backed values requires recreating the container.

Missing, unreadable or malformed tokens fail locally without retrying that credential error. HTTP 401/403 responses are ordinary download failures; synchronous/asynchronous failure policy still applies. cdh does not write tokens to its state or own logs, but does not isolate secrets from trusted same-user code.

## SSH access

SSH is disabled by default and provides root access when enabled. Prefer container-start credentials over baking them into the image. Set `SSH_ENABLE=true` and at least one valid `SSH_PUB_KEY` or `SSH_PASSWORD`; credentials alone do not enable SSH. Docker owns publication of the container's SSH port.

Copy a plain public-key line from an OpenSSH `.pub` file. Security-key Ed25519 and ECDSA P-256 keys are supported; certificates and `authorized_keys` prefixes such as `restrict`, `from=` or `command=` are not accepted. TOML key lists replace earlier lists, while `SSH_PUB_KEY` appends one key and ignores an existing key identity.

| Condition | Outcome |
| --- | --- |
| Enabled without a valid credential | Warn and continue without SSH |
| Enabled with credentials, but setup fails | Fail container startup |
| sshd exits after ComfyUI starts | Warn; ComfyUI continues |

The container creates its own host keys. Authenticated root sessions inherit the container-start environment, including credentials placed there; per-connection `TERM` and `SSH_AUTH_SOCK` remain OpenSSH-owned. SSH is not a secret-isolation boundary.

A login shell that reads the system profile enters `WORKSPACE`, falling back to `/root` with a warning if unavailable. A plain remote command such as `ssh root@host pwd` does not read that profile and starts in `/root`.

Prefer runtime configuration for public keys. If mounting `.ssh`, mount the safely owned parent directory rather than `authorized_keys` alone: cdh replaces the file atomically and a direct file mount can prevent replacement. Existing root-owned entries must not be group/world writable; unsafe links, ownership, read-only mounts or other write failures stop startup. cdh creates `.ssh` with `0700` and writes keys with `0600`; safe existing looser modes warn.

## Runtime hooks and startup readiness

Bake hooks with `--runtime-hooks-dir`, or mount deployment scripts under `/etc/cdh/runtime/hooks`. Each source has `pre-start.d`, `post-start.d` and `stop.d`. Only direct regular `.sh` and `.py` files run; other ordinary entries are ignored with warnings, while unsafe entries or read failures stop startup.

Baked hooks run before mounted hooks, in lexical filename order within each phase. Shell scripts use Bash and Python scripts use the application interpreter, with `COMFYUI_PATH` as their working directory. Both are trusted code; mounted hooks are outside the image lock. See [examples](../../examples/runtime-hooks/).

Startup and accepted restart follow this order:

1. Finish synchronous downloads.
2. Run pre-start hooks and start optional SSH.
3. Accept the asynchronous download queue and start ComfyUI.
4. If post-start hooks exist, wait for readiness and run them.

Readiness checks the loopback `/system_stats` response for HTTP 200 and the expected JSON shape. Process exit or timeout fails that startup path. Without post-start hooks, cdh does not wait for this probe. It is not continuous health monitoring or proof that a model or GPU workflow works.

If a hook starts a background service and exits, cdh no longer supervises that service. Pair it with a stop hook that requests shutdown through the service's own interface or a validated process identity and waits for exit. Container teardown alone does not guarantee graceful service shutdown.

## Signals and shutdown

Docker's stop signal reaches cdh through Tini. On the first `SIGTERM` or `SIGINT`, cdh cancels downloads and SSH, runs stop hooks while ComfyUI is available, signals ComfyUI, and waits for its managed processes to exit.

| Setting or event | Effect |
| --- | --- |
| `shutdown_timeout` (default 8 seconds) | Total shutdown/restart budget, with the final two seconds reserved for signaling ComfyUI and reaping managed children |
| Hook time expires | Terminate the current hook and skip later hooks |
| Total time expires, or a second signal arrives | Force shutdown; force-killed ComfyUI normally yields exit code 137 |
| Docker stop during application restart | Shutdown wins; ComfyUI is not started again |
| Natural ComfyUI exit | Preserve its result and clean up auxiliary work; signal-only stop hooks do not run |
| `shutdown_timeout = -1` | Disable cdh's outer/hook deadlines, not component bounds or Docker's deadline |

Set Docker's `--stop-timeout` or Compose's `stop_grace_period` longer than the cdh budget; the earlier example uses 30 seconds outside a 20-second cdh budget. The external hard limit is independent. No cleanup can continue after `SIGKILL`.
