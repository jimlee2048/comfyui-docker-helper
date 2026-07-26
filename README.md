# ComfyUI Docker Helper

`comfyui-docker-helper` (`cdh`) builds customized, CUDA-backed ComfyUI images
from declarative TOML. It manages the selected Python and PyTorch environment,
the official ComfyUI checkout, optional Manager and comfy-cli capabilities,
custom nodes, files, and lifecycle hooks.

## Requirements

- Python 3.12, 3.13, or 3.14 to run cdh.
- Docker when canonical resolution needs a new uv-backed result, and Docker
  with Buildx to build an image.
- A Linux x86_64 (`linux/amd64`) image target. GPU execution also requires
  NVIDIA Container Toolkit support, an NVIDIA driver `>=580.65.06`, and a
  Turing-or-newer NVIDIA GPU.

CUDA, PyTorch, and ComfyUI versions are explicit configuration selections.

## Install

```bash
uv tool install comfyui-docker-helper
cdh --help
```

Plain `pip install comfyui-docker-helper` is also supported. cdh does not
require a host uv executable; uv-backed canonical resolution runs through
Docker.

## Quick start

From a repository checkout, validate the
[minimal configuration](examples/minimal.toml), then build and load an image:

```bash
cdh host validate -f examples/minimal.toml
cdh host build -f examples/minimal.toml -t my-comfy:dev --load
```

Validation is local and offline. The build uses `.cdh/build/current` as its
managed context directory.

## Documentation

- [Configuration](docs/user/configuration.md) explains examples, layering,
  supported selections, and optional application, tool, node, and hook inputs.
- [Build and lock](docs/user/build-and-lock.md) covers validation, rendering,
  building, reconciliation modes, and generated artifacts.
- [Runtime](docs/user/runtime.md) covers container configuration, downloads,
  hooks, SSH, and lifecycle behavior.
- [Developer documentation](docs/dev/README.md) covers contribution workflow,
  architecture, cross-module contracts, and documentation governance.
- The [documentation index](docs/README.md) links every user and developer
  guide currently available.
- The [testing handbook](tests/README.md) covers test layers, cost
  authorization, and acceptance resources.

Run `cdh --help` or `cdh host --help` for current command help.

## License

[MIT](LICENSE)
