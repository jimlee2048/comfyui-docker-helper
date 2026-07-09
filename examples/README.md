# Examples

These examples are user-facing starting points for ComfyUI Docker Helper
configuration. They are separate from internal test fixtures under
`tests/fixtures/`.

## Files

- `minimal.toml` is a small copyable configuration for the minimal practical
  CUDA compute-platform build path.
- `full.toml` is an annotated reference configuration covering the supported
  host build blocks, baked runtime defaults, and commonly customized fields.
- `scripts/pre.sh` and `scripts/post.sh` are example custom-node hook scripts
  referenced by `full.toml`.

## Validate

Validate a configuration without writing a build context:

```bash
cdh host validate -f examples/minimal.toml
cdh host validate -f examples/full.toml --scripts-dir examples/scripts
```

## Render

Render an inspectable Docker build context:

```bash
cdh host render -f examples/minimal.toml -o .cdh/build/minimal --overwrite
cdh host render -f examples/full.toml -o .cdh/build/full --scripts-dir examples/scripts --overwrite
```

The rendered context is retained after render and build commands so you can
inspect generated files such as `Dockerfile`, `config.toml`,
`config.lock.toml`, `runtime/config.toml`, and copied `scripts/` content. Only
directories with a valid `.cdh-rendered` marker are automatically replaced by
`--overwrite`.

## Build

Real builds are intentionally resource-heavy. They pull base images, install
Python packages, clone ComfyUI/custom-node sources, and download configured
files over the network.

```bash
cdh host build -f examples/minimal.toml -t comfyui-example:minimal --load --context-dir .cdh/build/minimal
cdh host build -f examples/full.toml -t comfyui-example:full --scripts-dir examples/scripts --context-dir .cdh/build/full
cdh host build -f examples/full.toml --scripts-dir examples/scripts --context-dir .cdh/build/full
cdh host build -f examples/full.toml -t registry.example.com/my-comfy:dev --push --scripts-dir examples/scripts --context-dir .cdh/build/full
```

`full.toml` demonstrates `[cdh]` downloader defaults, `[build]` image tags,
`[build].output`, Python and PyTorch index URLs, structured `[comfyui]`
startup fields, custom-node hooks, baked runtime defaults, and file downloads.
CLI `--tag` values replace `[build].tags`; repeat `--tag` to build multiple
effective tags from the same config.
Registry authentication for `--push` is Docker/Buildx state, not cdh config;
sign in with tools such as `docker login` before pushing private images.

Use `--locked` to rebuild from an existing `config.lock.toml`, or
`--upgrade-lock` to refresh moving selectors before rendering and building:

```bash
cdh host build -f examples/full.toml --locked --scripts-dir examples/scripts --context-dir .cdh/build/full
cdh host render -f examples/full.toml -o .cdh/build/full --upgrade-lock --scripts-dir examples/scripts --overwrite
```

Do not run these build commands unless Docker Buildx is available and network,
disk, and CUDA base image/runtime requirements are acceptable for your machine.

When using hook scripts, pass `--scripts-dir` to the directory containing the
referenced script paths. Keep unrelated sensitive files out of that directory,
because referenced hooks cause the scripts directory to be copied into the
rendered build context.

## Runtime Defaults

Rendered images start through `ENTRYPOINT ["cdh", "container", "entrypoint"]`.
The render writes `runtime/config.toml` into the build context and bakes it to
`/opt/cdh/runtime/config.toml`. That runtime config contains only startup
fields (`[comfyui].listen`, `[comfyui].port`, `[comfyui].extra_args`),
downloader defaults/settings, retry and failure-policy settings,
`[system.ssh]`, and `[[files]]` entries.
Use `[cdh].default_download_mode = "sync"` or `"async"`, or per-file
`download_mode = "sync"` or `"async"`, when you want the runtime file mode to
be explicit in configuration.

At container startup, mount `/etc/cdh/runtime/config.toml` to override baked
runtime defaults. The supported startup environment overrides are
`CDH_COMFYUI_LISTEN`, `CDH_COMFYUI_PORT`, `CDH_COMFYUI_EXTRA_ARGS`,
`CDH_DEFAULT_DOWNLOADER`, `CDH_DEFAULT_DOWNLOAD_MODE`,
`CDH_DOWNLOAD_MAX_ATTEMPTS`, `CDH_DOWNLOAD_FAILURE_POLICY`, `SSH_ENABLE`,
`SSH_PORT`, `SSH_PASSWORD`, and `SSH_PUB_KEY`.

SSH is disabled by default. `[system.ssh]` can provide baked SSH defaults,
including a password or public keys, but baked credentials can appear in
rendered contexts and image artifacts. Prefer runtime environment credentials:
`SSH_PASSWORD` sets the root password at container startup, and `SSH_PUB_KEY`
appends one OpenSSH public key line to root's `authorized_keys`. SSH login is
for `root`; enabling it exposes root access on the container-internal SSH port.
Docker host port publishing, network access controls, and ComfyUI
authentication are deployment responsibilities. Do not bake real SSH passwords
or private credentials into image configuration.

`[[files]]` entries are downloaded during image build and are processed again
at startup from the effective runtime config. Host build downloads always run
synchronously. Startup downloads use the effective runtime scheduling mode:
`sync` downloads block pre-start hooks and ComfyUI startup, while `async`
downloads run in a background queue without blocking ComfyUI readiness or
post-start hooks after the queue starts.

Runtime downloads use `/var/lib/cdh/runtime/state.json` for restart
reconciliation, per-start retry accounting, and completed-source tracking. Use
a persistent volume for `/var/lib/cdh/runtime` when that state should survive
container replacement. Partial downloads are staged beside their final target
under `.cdh-staging/`; cdh only cleans up stale files that use its own staging
filename pattern. Existing targets are skipped unless `overwrite = true`, and
`overwrite = true` reuses completed runtime state instead of redownloading on
every start.

`download_failure_policy = "continue"` records exhausted runtime download
failures and continues with later files. `download_failure_policy = "fail"`
stops startup for exhausted sync downloads; for async downloads it stops
scheduling later async files for the current start but does not terminate an
already-running ComfyUI process.

Runtime lifecycle hooks are supplied with `--hooks-dir`. The hook root may
contain `pre-start.d/`, `post-start.d/`, and `stop.d/` directories with regular
`.sh` or `.py` files. Pre-start hooks run after synchronous runtime downloads
and before ComfyUI starts. Post-start hooks run only after ComfyUI responds on
`/system_stats`. Stop hooks run during graceful shutdown before the original
signal is forwarded to ComfyUI.

Values in `[system.env]` are written as Dockerfile `ENV` values. They may appear
in image history, generated contexts, and build logs. Do not place secrets in
configuration unless that exposure is acceptable.
