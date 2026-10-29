# ComfyUI Docker Helper

English | [简体中文](README.zh-CN.md)

[![CI](https://github.com/jimlee2048/comfyui-docker-helper/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/jimlee2048/comfyui-docker-helper/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)
[![PyPI version](https://img.shields.io/pypi/v/comfyui-docker-helper.svg)](https://pypi.org/project/comfyui-docker-helper/)
[![Python versions](https://img.shields.io/pypi/pyversions/comfyui-docker-helper.svg)](https://pypi.org/project/comfyui-docker-helper/)

> [!IMPORTANT]
>
> - This is an independent, unofficial project. It is not affiliated with or endorsed by the ComfyUI project.
> - This project is in early development; features and configuration are not guaranteed to remain stable.
> - All code, tests, and documentation in this project are produced by AI coding agents, with humans providing requirements, direction, and review.

`comfyui-docker-helper` (`cdh`) is a command-line helper for using ComfyUI with Docker.

## Features

- Validate and layer declarative TOML configurations.
- Resolve and lock selected Python, PyTorch, ComfyUI, tool, custom-node, and remote-file inputs.
- Build customized CUDA-backed ComfyUI images for Linux `amd64` with Docker Buildx.
- Use the host's default SSH agent to build private direct-Git custom nodes.
- Authenticate private direct-Git custom nodes over HTTP(S) with configured credentials.
- Publish one build under deterministic tags derived from its accepted ComfyUI release or commit.
- Add optional Manager, comfy-cli, custom nodes, files, downloads, lifecycle hooks, and runtime SSH access.

## Host requirements

To build images with cdh, the host needs:

- Python 3.12, 3.13, or 3.14.
- A working Docker installation with Buildx configured to build Linux containers. On Windows, normally use Docker Desktop in Linux container mode; another endpoint must provide equivalent Linux `amd64` Buildx support. Windows containers are not supported.
- Git on `PATH` when cdh must resolve Git-backed sources, including ComfyUI selectors that query the repository and direct-Git custom nodes. On Windows, install Git for Windows.

`cdh host *` runs natively on Linux x86_64/ARM64, Windows x86_64, and macOS ARM64. The generated ComfyUI images still target Linux `amd64`, and `cdh container *` runs inside those Linux images. The standard `uv tool` and `pip` commands below select the required platform dependencies automatically.

See [Build and lock](docs/user/build-and-lock.md#host-and-target-platforms) for Docker requirements on each host platform.

## Install

Install with `uv tool` (recommended):

```bash
uv tool install comfyui-docker-helper
```

Installation with `pip` is also supported:

```bash
pip install comfyui-docker-helper
```

Run `cdh --help` after installation.

## Quick start

### Build an image

Save the repository's [minimal configuration](examples/minimal.toml) as `cdh.toml`, then validate it and build a local image:

```bash
cdh host validate -f cdh.toml
cdh host build -f cdh.toml -t my-comfy:dev --load
```

A successful build loads `my-comfy:dev` into the local Docker image store. Next, follow [Start and stop ComfyUI](docs/user/runtime.md#start-and-stop-comfyui) to launch the container, open its UI, inspect startup and stop it.

GPU execution requires a host meeting the [runtime requirements](docs/user/runtime.md#gpu-host-requirements).

## Documentation

- [Configuration](docs/user/configuration.md) explains examples, layering, supported selections, and optional application, tool, node, and hook inputs.
- [Build and lock](docs/user/build-and-lock.md) covers validation, rendering, building, lock choices, private inputs, and reusable contexts.
- [Runtime](docs/user/runtime.md) covers first startup, settings, persistence, restart, logs, and shutdown.
- [Developer documentation](docs/dev/README.md) covers contribution workflow, architecture, cross-module contracts, and documentation governance.
- The [documentation index](docs/README.md) links every user and developer guide currently available.
- The [testing handbook](tests/README.md) covers test layers, cost authorization, and acceptance resources.

Run `cdh --help` or `cdh host --help` for current command help.

## License

[MIT](LICENSE)
