# Examples

These examples are user-facing starting points for ComfyUI Docker Helper
configuration. They are separate from internal test fixtures under
`tests/fixtures/`.

## Files

- `minimal.toml` is a small copyable configuration for the minimal practical
  CUDA compute-platform build path.
- `full.toml` is an annotated reference configuration covering every supported
  block and field.
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
inspect generated files such as `Dockerfile`, `config/custom-nodes.toml`,
`config/files.toml`, and copied `scripts/` content. Only directories with a
valid `.cdh-rendered` marker are automatically replaced by `--overwrite`.

## Build

Real builds are intentionally resource-heavy. They pull base images, install
Python packages, clone ComfyUI/custom-node sources, and download configured
files over the network.

```bash
cdh host build -f examples/minimal.toml -t comfyui-example:minimal --context-dir .cdh/build/minimal
cdh host build -f examples/full.toml -t comfyui-example:full --scripts-dir examples/scripts --context-dir .cdh/build/full
```

Do not run these build commands unless Docker Buildx is available and network,
disk, and CUDA base image/runtime requirements are acceptable for your machine.

When using hook scripts, pass `--scripts-dir` to the directory containing the
referenced script paths. Keep unrelated sensitive files out of that directory,
because referenced hooks cause the scripts directory to be copied into the
rendered build context.

Values in `[system.env]` are written as Dockerfile `ENV` values. They may appear
in image history, generated contexts, and build logs. Do not place secrets in
configuration unless that exposure is acceptable.
