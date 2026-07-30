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

cdh 会先在同级私有暂存目录中完成上下文，再进行发布。首次发布会将这棵完整暂存树重命名到目标位置。使用 `--overwrite` 时，cdh 会先将旧上下文移动到唯一的同级备份，再将完整暂存树重命名到目标位置；如果第二次重命名失败，cdh 会在当前进程内尝试恢复旧上下文。这种可移植的覆盖流程既不保证目标路径始终无间隙地存在，也不保证崩溃后的持久恢复。如果发布及其进程内恢复均报告文件系统错误，诊断会指出保留旧上下文的备份路径。进程或宿主机在两次重命名之间中断时，则可能导致目标路径缺失，而完整的旧上下文留在唯一的同级备份中，且不提供恢复保证。

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

当 direct-Git 自定义节点或其递归 submodule 需要宿主机上的 SSH 身份时，使用 `--ssh`。构建前，请将所需身份加载到默认 SSH agent，并将服务器主机密钥加入默认的 OpenSSH known-hosts 文件。

```bash
cdh host build \
  -f examples/full.toml \
  --context-dir .cdh/build/current \
  -t my-comfy:dev \
  --load \
  --ssh
```

该选项要求 `SSH_AUTH_SOCK` 非空。cdh 使用默认 agent 以及现有的默认用户和系统 known-hosts 文件；它不会检查 agent、添加主机密钥、复制私钥或读取 `~/.ssh/config`。对于自托管服务或非默认端口，请使用 `ssh://git@example.test:2222/group/node.git` 之类的显式 locator，并将对应的主机及端口条目加入默认 known-hosts 文件。该选项不支持 SSH alias、`ProxyJump`、自定义密钥或信任文件 selector、原始密钥文件以及 HTTPS token 鉴权。

如果生效配置没有 direct-Git 节点，`--ssh` 会输出一次 warning，并在不转发任何内容的情况下继续。否则，缺少 agent 会在解析源码前失败；主机密钥、鉴权以及 Git/submodule 的失败则会保留其底层诊断。

请将配置的自定义节点 Hook 和安装程序视为受信任代码：在安装自定义节点期间，它们能够使用转发的 agent 并读取提供的信任文件。私钥字节仍由 agent 持有，cdh 不会自动将 agent 或信任文件放入渲染上下文或镜像。缓存的自定义节点层也可能在不联系当前 agent 或不重新检查已更新信任的情况下被复用；需要重新验证鉴权时，请绕过相应的 BuildKit 缓存。

### 直接构建渲染上下文

渲染后的 direct-Git 上下文也可以直接通过 Buildx 构建。请提供默认 SSH agent，并为构建所需的每个 known-hosts 文件提供一个 secret。以下示例使用最常见的用户信任文件：

```bash
docker buildx build \
  --ssh default \
  --secret "type=file,id=cdh-ssh-known-hosts-user,src=$HOME/.ssh/known_hosts" \
  --load \
  -t my-comfy:dev \
  .cdh/build/current
```

渲染后的 Dockerfile 会显示其他受支持默认信任文件各自的稳定 secret ID。请省略源文件不存在的 secret。

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
| 默认 | 复用未变更的条目，解析缺失或已变更的输入，并移除已删除的身份。 | 发布包含已接受 lock 的完整暂存上下文。覆盖时采用上文所述的可移植备份与重命名流程。 |
| `--locked` | 要求现有 lock 与本地输入完全匹配；协调期间不调用解析提供方或 Docker。 | 比较现有上下文且不写入任何内容。检查通过后，`host build` 仍会调用 Buildx。 |
| `--upgrade-lock` | 刷新浮动选择器，同时保留未变更的精确选择。 | 发布包含更新后 lock 的完整暂存上下文。覆盖时采用上文所述的可移植备份与重命名流程。 |
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
