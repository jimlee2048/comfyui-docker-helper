# ComfyUI Docker Helper

[English](README.md) | 简体中文

`comfyui-docker-helper`（`cdh`）根据声明式 TOML 配置构建定制的、
支持 CUDA 的 ComfyUI 镜像。它负责管理选定的 Python 和 PyTorch 环境、
ComfyUI 官方检出版本、可选的 Manager 和 comfy-cli 功能、自定义节点、
文件以及生命周期 Hook。

## 环境要求

- 运行 cdh 需要 Python 3.12、3.13 或 3.14。
- 当规范化解析需要生成新的 uv 结果时需要 Docker；构建镜像还需要
  支持 Buildx 的 Docker。
- 镜像目标为 Linux x86_64（`linux/amd64`）。使用 GPU 运行还需要
  NVIDIA Container Toolkit、版本不低于 `580.65.06` 的 NVIDIA
  驱动，以及 Turing 或更新架构的 NVIDIA GPU。

CUDA、PyTorch 和 ComfyUI 版本都需要在配置中显式选择。

## 安装

```bash
uv tool install comfyui-docker-helper
cdh --help
```

也支持直接使用 `pip install comfyui-docker-helper`。cdh 不要求宿主机安装
uv 可执行文件；基于 uv 的规范化解析通过 Docker 运行。

## 快速开始

在仓库检出目录中，先验证
[最小配置](examples/minimal.toml)，然后构建并加载镜像：

```bash
cdh host validate -f examples/minimal.toml
cdh host build -f examples/minimal.toml -t my-comfy:dev --load
```

验证过程在本地离线完成。构建使用 `.cdh/build/current` 作为由 cdh
管理的上下文目录。

## 文档

- [配置](docs/user/configuration.zh-CN.md)介绍示例、配置分层、支持的选择，
  以及可选的应用、工具、节点和 Hook 输入。
- [构建与锁定](docs/user/build-and-lock.zh-CN.md)介绍验证、渲染、构建、
  协调模式和生成的工件。
- [运行时](docs/user/runtime.zh-CN.md)介绍容器配置、下载、Hook、SSH
  和生命周期行为。
- [开发者文档（英文）](docs/dev/README.md)介绍贡献流程、架构、跨模块契约
  和文档治理。
- [中文文档索引](docs/README.zh-CN.md)链接当前可用的所有中文用户指南，
  以及尚未本地化的英文开发者指南。
- [测试手册（英文）](tests/README.md)介绍测试层级、成本授权和验收资源。

运行 `cdh --help` 或 `cdh host --help` 查看当前命令帮助。

## 许可证

[MIT](LICENSE)
