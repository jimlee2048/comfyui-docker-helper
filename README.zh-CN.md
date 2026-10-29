# ComfyUI Docker Helper

[English](README.md) | 简体中文

[![CI](https://github.com/jimlee2048/comfyui-docker-helper/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/jimlee2048/comfyui-docker-helper/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)
[![PyPI version](https://img.shields.io/pypi/v/comfyui-docker-helper.svg)](https://pypi.org/project/comfyui-docker-helper/)
[![Python versions](https://img.shields.io/pypi/pyversions/comfyui-docker-helper.svg)](https://pypi.org/project/comfyui-docker-helper/)

> [!IMPORTANT]
>
> - 本项目是独立开发的非官方工具，与 ComfyUI 项目没有隶属关系，也未获得其背书。
> - 本项目尚处于早期开发阶段，暂不保证功能和配置的稳定性。
> - 编码 AI 智能体主导大部分开发工作，人类负责提供整体方向指导。

`comfyui-docker-helper`（`cdh`）是一个用于通过 Docker 使用 ComfyUI 的命令行辅助工具。

## 主要功能

- 验证并分层组合声明式 TOML 配置。
- 解析并锁定选定的 Python、PyTorch、ComfyUI、工具、自定义节点和远程文件输入。
- 使用 Docker Buildx 构建面向 Linux `amd64`、支持 CUDA 的定制 ComfyUI 镜像。
- 使用宿主机的默认 SSH 代理构建私有 Git 自定义节点。
- 使用已配置的凭据通过 HTTP(S) 访问私有 Git 自定义节点。
- 根据所选 ComfyUI 版本或提交为同一次构建生成确定性的发布标签。
- 添加可选的 Manager、comfy-cli、自定义节点、文件、下载、生命周期钩子和运行时 SSH 访问。

## 宿主机要求

使用 cdh 构建镜像的宿主机需要：

- Python 3.12、3.13 或 3.14。
- 可正常使用、支持 Buildx 且配置为构建 Linux 容器的 Docker 环境。在 Windows 上，通常使用处于 Linux 容器模式的 Docker Desktop；其他端点必须提供等效的 Linux `amd64` Buildx 支持。不支持 Windows 容器。
- 当 cdh 需要解析 Git 来源时，需确保 `PATH` 中存在 Git；这包括需要查询仓库的 ComfyUI 版本选择和 Git 自定义节点。Windows 上请安装 Git for Windows。

`cdh host *` 可以在 Linux x86_64/ARM64、Windows x86_64 和 macOS ARM64 上原生运行。生成的 ComfyUI 镜像仍以 Linux `amd64` 为目标，`cdh container *` 在这些 Linux 镜像内运行。下方标准的 `uv tool` 和 `pip` 命令会自动选择所需的平台依赖。

自动化测试覆盖了原生宿主机行为，但不会运行 Docker Desktop 端到端构建，也不证明 Docker Desktop 的 SSH 代理转发。平台和输入安全边界详见[构建与锁定](docs/user/build-and-lock.zh-CN.md#宿主机与目标平台)。

## 安装

推荐使用 `uv tool` 安装：

```bash
uv tool install comfyui-docker-helper
```

也支持使用 `pip` 安装：

```bash
pip install comfyui-docker-helper
```

安装后运行 `cdh --help` 查看命令帮助。

## 快速开始

### 构建镜像

将仓库中的[最小配置](examples/minimal.toml)保存为 `cdh.toml`，然后验证配置并构建本地镜像：

```bash
cdh host validate -f cdh.toml
cdh host build -f cdh.toml -t my-comfy:dev --load
```

构建成功后，`my-comfy:dev` 会载入本地 Docker 镜像仓库。接着按[启动和停止 ComfyUI](docs/user/runtime.zh-CN.md#启动和停止-comfyui)运行容器、打开界面、检查启动结果并停止。

GPU 运行需要兼容的 NVIDIA 主机；能在 macOS 或 Windows 上构建，不代表该主机支持 GPU 运行。

## 文档

- [配置](docs/user/configuration.zh-CN.md)介绍示例、配置分层、支持的选择，以及可选的应用、工具、节点和钩子设置。
- [构建与锁定](docs/user/build-and-lock.zh-CN.md)介绍验证、构建、锁定选择、私有来源和可复用构建目录。
- [运行指南](docs/user/runtime.zh-CN.md)介绍首次启动、设置、持久化、重启、日志和关闭。
- [开发者文档（英文）](docs/dev/README.md)介绍贡献流程、架构、跨模块契约和文档治理。
- [中文文档索引](docs/README.zh-CN.md)链接当前可用的所有中文用户指南，以及尚未本地化的英文开发者指南。
- [测试手册（英文）](tests/README.md)介绍测试层级、成本授权和验收资源。

运行 `cdh --help` 或 `cdh host --help` 查看当前命令帮助。

## 许可证

[MIT](LICENSE)
