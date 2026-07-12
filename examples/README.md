# Configuration examples

`minimal.toml` is the smallest canonical CUDA 13 / PyTorch 2.12 configuration.
`full.toml` documents every field in the current public configuration schema.

Validate locally without providers or writes:

```bash
cdh host validate -f examples/minimal.toml
cdh host validate -f examples/full.toml --scripts-dir examples/scripts
```

Render with canonical reconciliation:

```bash
cdh host render \
  -f examples/minimal.toml \
  -o .cdh/build/minimal \
  --hooks-dir examples/hooks \
  --overwrite
```

The output contains canonical `config.lock.toml`, `build-plan.json`,
`manifest-binding.json`, narrow `phases/*.json`, a BuildPlan-derived runtime
config, verified referenced build-hook bytes, content-locked baked runtime
hooks, and a Dockerfile with literal digest-qualified `FROM` references. It
does not copy root config into the container-helper authority.

The optional `--hooks-dir` tree may contain only regular `.sh` or
`.py` files directly under `pre-start.d/`, `post-start.d/`, and `stop.d/`.
Every baked hook is content-locked and copied to `/opt/cdh/runtime/hooks`;
mounted `/etc/cdh/runtime/hooks` remains external runtime input.

Use `--dry-run` for an exact no-write preview, `--check` to compare an existing
context, `--locked` for zero-provider/zero-write verification, and
`--upgrade-lock` to refresh moving selectors.

The public PyTorch version is a selector. Its CUDA-derived channel, index, and
target enter the resolver request identity, while the canonical lock and
BuildPlan retain complete resolved versions such as `2.12.1+cu130`.
