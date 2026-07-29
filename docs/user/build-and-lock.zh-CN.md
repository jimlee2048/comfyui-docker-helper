# 构建和锁定镜像

[English](build-and-lock.md) | 简体中文

本指南介绍本地验证、canonical lock 协调、渲染构建上下文以及 Docker 镜像构建。请先阅读[配置指南](configuration.zh-CN.md)，以选择和分层配置文件。以下命令均从仓库根目录运行。

## 验证、渲染和构建

在解析或构建任何内容之前验证配置：

```bash
cdh host validate -f examples/minimal.toml
```

验证在本地完成：它不会调用解析提供方或 Docker，也不会写入文件。可以重复使用 `-f/--file` 来指定配置层；cdh 按命令行中的顺序合并这些配置层，并验证最终生效的结果。

渲染可复用的构建上下文和 canonical lock：

```bash
cdh host render \
  -f examples/minimal.toml \
  -o .cdh/build/current \
  --overwrite
```

渲染会复用匹配的 lock。当缺失或已变更的基于 uv 的解析结果必须重新生成时，它可能会使用 Docker。`--overwrite` 只会替换现有且有效的 cdh 管理上下文；否则 cdh 会拒绝替换。

使用 Docker Buildx 构建镜像：

```bash
cdh host build \
  -f examples/minimal.toml \
  --context-dir .cdh/build/current \
  -t my-comfy:dev \
  --load
```

`host build` 会渲染所选上下文，然后调用 Buildx。请提供一个或多个 `-t/--tag` 值，或者配置 `[build].tags`。使用 `--load` 将镜像载入本地 Docker 镜像存储，或使用 `--push` 推送至 registry；这两个选项互斥。

## 通过 SSH 访问私有 Git 自定义节点

当 direct-Git 自定义节点、其递归 submodule 或 Git URL rewrite 需要宿主机的默认 SSH 身份时，使用 `--ssh`：

```bash
cdh host build \
  -f examples/full.toml \
  --context-dir .cdh/build/current \
  -t my-comfy:dev \
  --load \
  --ssh
```

该选项要求 `SSH_AUTH_SOCK` 非空，并转发 BuildKit 的默认 SSH agent 输入。cdh 还会提供宿主机上当前存在的以下默认 OpenSSH 信任文件：`~/.ssh/known_hosts`、`~/.ssh/known_hosts2`、`/etc/ssh/ssh_known_hosts` 和 `/etc/ssh/ssh_known_hosts2`。cdh 不检查 agent、不解析信任条目、不检查目标覆盖范围，也不添加主机密钥。请在构建前于宿主机上准备好 agent 和信任。对于自托管服务或非默认端口，请使用 `ssh://git@example.test:2222/group/node.git` 之类的显式 locator，并在默认 known-hosts 文件中加入对应的主机及端口条目。

渲染后的 direct-Git Dockerfile 声明的是可选 mount，因此不使用 `--ssh` 时仍可构建公开的 HTTPS 仓库。当该选项生效时，agent 和挂载的信任会在整个按声明顺序执行的自定义节点安装 `RUN` 中可用，其中包括选定的 pre/post hooks 和安装程序。请将所有配置的节点及 Hook 代码视为受信任代码。私钥字节仍由 agent 持有，cdh 和 BuildKit 不会自动将 agent 或信任输入复制到渲染上下文或镜像层；但该指令内的受信任代码仍可读取并主动复制或披露挂载的信任文件。

cdh 会为这条指令禁用容器内的环境 SSH client 配置，并且只依据挂载的默认信任文件执行严格的主机密钥检查。该选项不支持 SSH alias、`ProxyJump`、自定义 `IdentityFile` 或 `UserKnownHostsFile` selector、复制原始密钥以及 HTTPS PAT/token 鉴权。HTTPS 根 locator 仍适用，因为递归 submodule 或 URL rewrite 可能使用 SSH。

若要直接构建渲染上下文，请为每个实际存在的默认信任文件提供等效的 Buildx 输入。例如：

```bash
docker buildx build \
  --ssh default \
  --secret "type=file,id=cdh-ssh-known-hosts-user,src=$HOME/.ssh/known_hosts" \
  --secret "type=file,id=cdh-ssh-known-hosts-user-legacy,src=$HOME/.ssh/known_hosts2" \
  --secret "type=file,id=cdh-ssh-known-hosts-system,src=/etc/ssh/ssh_known_hosts" \
  --secret "type=file,id=cdh-ssh-known-hosts-system-legacy,src=/etc/ssh/ssh_known_hosts2" \
  --load \
  -t my-comfy:dev \
  .cdh/build/current
```

如果某个 `--secret` 的源不存在，请将其省略。如果在生效配置没有 direct-Git 节点时传入 `--ssh`，cdh 会输出一次 warning，并在不转发 SSH 输入的情况下继续。适用的构建若缺少非空的 `SSH_AUTH_SOCK`，会在 provider 工作之前失败。provider、Buildx 源接纳、主机密钥、鉴权以及 Git/submodule 的其他失败均保留其底层诊断。SSH agent 或 secret 内容发生变化时，BuildKit 通常不会使缓存的 `RUN` 失效，因此缓存命中可能会复用之前已完成的自定义节点层，而不联系当前 agent 或重新检查当前信任。

## Hook 源目录

自定义节点配置所引用的 build hooks 没有隐式源目录。请向 `validate`、`render` 和 `build` 传入 `--build-hooks-dir`：

```bash
cdh host validate \
  -f examples/full.toml \
  --build-hooks-dir examples/build-hooks
```

配置中的路径相对于此目录。cdh 只接纳被引用的常规 `.sh` 和 `.py` 文件，并保留它们安全的相对布局。构建 Hook 是受信任代码，其经过验证的源字节会保留在最终镜像及其镜像层中。请勿在其中放入机密信息。参见[构建 Hook 示例](../../examples/build-hooks/)。

向 `render` 或 `build` 传入 `--runtime-hooks-dir`，以烘焙 runtime hook 目录树：

```bash
cdh host render \
  -f examples/full.toml \
  -o .cdh/build/full \
  --build-hooks-dir examples/build-hooks \
  --runtime-hooks-dir examples/runtime-hooks \
  --overwrite
```

运行时 Hook 目录只能包含直接位于 `pre-start.d/`、`post-start.d/` 和 `stop.d/` 下的受支持 Hook 文件。省略该选项时不会烘焙运行时 Hook 目录树。挂载的运行时 Hook 是独立的部署时输入；参见[运行时指南](runtime.zh-CN.md)和[运行时 Hook 示例](../../examples/runtime-hooks/)。

## 从生效配置到上下文

cdh 使用单向前进的规划流程：

```text
effective configuration -> canonical lock -> BuildPlan -> rendered context
```

生效配置描述意图。`config.lock.toml` 记录宿主机协调所使用并已接受的外部内容和本地内容的精确身份。随后，cdh 构造一个不可变的 BuildPlan，作为构建时执行权威。上下文渲染会将该计划连同其精确 wheel 和经过验证的 Hook 输入一起投影出来；构建时辅助程序不会重新读取宿主机配置或 lock 来作出新的规划决策。

## 协调模式

解析提供方策略与文件系统/构建副作用彼此独立。可以从以下五种面向用户的模式中选择：

| 模式 | 解析行为 | 上下文和构建行为 |
| --- | --- | --- |
| 默认 | 复用未变更的条目，解析缺失或已变更的输入，并移除已删除的身份。 | 以原子方式写入已接受的 lock 和渲染上下文。 |
| `--locked` | 要求现有 lock 与本地输入完全匹配；协调期间不调用解析提供方或 Docker。 | 比较现有上下文且不写入任何内容。检查通过后，`host build` 仍会调用 Buildx。 |
| `--upgrade-lock` | 刷新浮动选择器，同时保留未变更的精确选择。 | 以原子方式写入更新后的 lock 和上下文。 |
| `--check` | 应用默认协调策略。 | 将完整的预期上下文与现有上下文进行比较；不写入任何内容，也不构建。 |
| `--dry-run` | 使用默认策略；与 `--locked` 或 `--upgrade-lock` 组合使用时除外。 | 输出精确的 BuildPlan 预览；不写入任何内容，也不构建。 |

`--check` 不能与 lock 策略或 dry-run 修饰选项组合使用。`--locked` 与 `--upgrade-lock` 互斥。当 `--dry-run` 与 lock 策略组合使用时，预览行为会取代上下文比较或发布。

不写入并不一定意味着离线。默认、`--check` 和 `--dry-run` 可能会调用解析提供方；当当前 lock 无法提供所需的基于 uv 的解析结果时，它们可能还需要 Docker。完整且匹配的 lock 可使这些路径无需 Docker。只有 `--locked` 禁止在协调期间调用解析提供方和 Docker；Docker Buildx 仍是 `host build` 的一项独立要求。

格式错误或不受支持的 lock 文件会以失败关闭，并给出诊断信息，提示删除并重新生成 lock。

## 渲染上下文

渲染上下文包含：

- `.cdh-rendered`，cdh 管理上下文的宿主机标记；
- `config.lock.toml`，仅供宿主机使用的协调状态；
- `build-plan.json`，规范的构建时执行计划；
- `bootstrap/comfyui_docker_helper-<version>-py3-none-any.whl`，安装到镜像中且经过精确验证的 cdh wheel；
- `build/hooks/`，配置后仅包含被引用且经过验证的构建 Hook 字节；
- `runtime/config.toml`，派生自 BuildPlan；
- `runtime/hooks/`，配置后包含经过验证且已烘焙的运行时 Hook 目录树；
- `Dockerfile`，使用字面量且带 digest 的基础镜像引用渲染而成；以及
- `.dockerignore`，将 `config.lock.toml` 和 `.cdh-rendered` 排除在 Buildx 输入之外。

上下文不包含根级 `config.toml`。宿主机本地源路径不是 BuildPlan 输入，并且 Dockerfile 没有能够替换由 lock 定权的镜像身份的参数。

## Python 环境和包源

镜像将应用软件包和用户工具保持在彼此独立的所有权域中：

- `/opt/venv` 包含 ComfyUI、其应用依赖项，以及可选且由检出版本拥有的 Manager/`cm-cli` 功能。
- cdh、可选的 `comfy-cli`，以及每个配置的 `[python].uv_tools` 软件包，分别使用 `/opt/uv/tools` 下的独立环境。
- 工具命令链接在 `/opt/uv/bin` 下；发生可执行命令所有权冲突时会失败，而不是替换现有命令。

`comfy-cli` 是可选的用户工具，在镜像构建期间不会用于安装 ComfyUI、Manager 或 Registry 自定义节点。

由 cdh 控制的普通 Python 解析和安装使用 `[python].index_url`。直接 PyTorch 软件包与选定 ComfyUI 检出版本中在目标环境生效的受保护依赖要求共同构成一个精确分组，并且只使用由 CUDA 派生的 PyTorch 软件源。它们的普通传递依赖项使用普通 Python 源。缺失的直接 PyTorch 成员不会回退到普通源上的同名软件包，并且选定的精确分组受到保护，不会被后续由 cdh 控制的应用变更改动。

## 最终证据和重放边界

所有镜像变更成功后，cdh 会写入严格的最终状态观测 `/opt/cdh/build/manifest.json`。它绑定生效配置、canonical lock 和 BuildPlan 的 digest，并记录预期和观测到的直接身份。该 manifest 是证据，而不是另一个解析器、lock、重放输入、支持性结论或一般性的服务健康检查。

cdh 为由 cdh 控制的直接输入提供有界且经过验证的重放。这并不承诺离线构建或字节完全一致的构建，也不承诺对传递依赖项或每个已获取工件进行完整锁定，不为缺少用户所提供 checksum 的下载内容提供真实性保证，不保证受信任安装程序或 Hook 的效果具有确定性，也不承诺重放部署时变更。
