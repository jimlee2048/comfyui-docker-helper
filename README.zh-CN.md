# ComfyUI Docker Helper

[English](README.md) | 简体中文

[![CI](https://github.com/jimlee2048/comfyui-docker-helper/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/jimlee2048/comfyui-docker-helper/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)
[![PyPI version](https://img.shields.io/pypi/v/comfyui-docker-helper.svg)](https://pypi.org/project/comfyui-docker-helper/)
[![Python versions](https://img.shields.io/pypi/pyversions/comfyui-docker-helper.svg)](https://pypi.org/project/comfyui-docker-helper/)

> [!IMPORTANT]
>
> - 本项目是独立开发的非官方工具，与 ComfyUI 项目没有隶属关系，也未获得其背书。
> - 本项目尚处于早期开发阶段，暂不保证功能和配置的稳定性。
> - Coding AI Agents 主导大部分开发工作，人类负责提供整体方向指导。

`comfyui-docker-helper`（`cdh`）是一个用于通过 Docker 使用 ComfyUI 的命令行辅助工具。

## 主要功能

- 验证并分层组合声明式 TOML 配置。
- 解析并锁定选定的 Python、PyTorch、ComfyUI、工具、自定义节点和远程文件输入。
- 使用 Docker Buildx 构建面向 Linux `amd64`、支持 CUDA 的定制 ComfyUI 镜像。
- 添加可选的 Manager、comfy-cli、自定义节点、文件、下载、生命周期 Hook 和 SSH 支持。

## 宿主机要求

使用 cdh 构建镜像的宿主机需要：

- Python 3.12、3.13 或 3.14。
- 可正常使用且支持 Buildx 的 Docker 环境。

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

构建成功后，`my-comfy:dev` 会载入本地 Docker 镜像存储。

## 文档

- [配置](docs/user/configuration.zh-CN.md)介绍示例、配置分层、支持的选择，以及可选的应用、工具、节点和 Hook 输入。
- [构建与锁定](docs/user/build-and-lock.zh-CN.md)介绍验证、渲染、构建、协调模式和生成的工件。
- [运行时](docs/user/runtime.zh-CN.md)介绍容器配置、下载、Hook、SSH 和生命周期行为。
- [开发者文档（英文）](docs/dev/README.md)介绍贡献流程、架构、跨模块契约和文档治理。
- [中文文档索引](docs/README.zh-CN.md)链接当前可用的所有中文用户指南，以及尚未本地化的英文开发者指南。
- [测试手册（英文）](tests/README.md)介绍测试层级、成本授权和验收资源。

运行 `cdh --help` 或 `cdh host --help` 查看当前命令帮助。

## 许可证

[MIT](LICENSE)
