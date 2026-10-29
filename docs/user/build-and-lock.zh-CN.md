# 构建和锁定镜像

规范性来源：[English](build-and-lock.md) | 简体中文

本指南帮助你从 `cdh.toml` 构建镜像、选择何时复用或更新锁定输入，并准备本地或私有来源。首次运行容器时，接着阅读[启动和停止 ComfyUI](runtime.zh-CN.md#启动和停止-comfyui)。

## 验证和构建

先验证配置，再构建并加载到本地 Docker 镜像仓库：

```bash
cdh host validate -f cdh.toml
cdh host build -f cdh.toml -t my-comfy:dev --load
```

验证在本地完成且不写入文件。构建会准备目录、按需解析输入，再调用 Docker Buildx。两条命令应使用相同的 `-f` 层顺序和钩子目录选项。

需要将构建目录保留在指定位置时：

```bash
cdh host render -f cdh.toml -o .cdh/build/current --overwrite
cdh host build -f cdh.toml --context-dir .cdh/build/current -t my-comfy:dev --load
```

`render` 准备构建目录和锁文件，不构建最终镜像，但解析输入仍可能需要网络和 Docker。`build` 自己也会准备目录，不必先执行 `render`。

`--load` 用于本地使用，`--push` 用于发布到镜像仓库，两者互斥。用 `-t` 或 `[build].tags` 指定标签。本地构建成功后，在运行指南中使用相同镜像标签。

`--overwrite` 只替换有效的 cdh 构建目录。cdh 会先准备完整替代内容，但替换过程中若被中断，输出目录可能暂时不存在，旧目录则留在旁边的备份位置。若诊断报告保留了备份，应先保留它用于恢复，再重试。

### 输出和详细度

详细度选项放在 `host` 之前，例如 `cdh -v host validate -f cdh.toml`。

| 选项 | 效果 |
| --- | --- |
| 默认 | 必要的进度和结果；非交互验证成功时可能没有输出 |
| `-q` | 隐藏 cdh 提示信息，保留警告、错误及显式请求的数据 |
| `-v` / `-vv` | 增加操作详情 / 调试详情 |

安静和详细模式不能同时使用。结果与 BuildKit 输出写入 stdout，cdh 进度与诊断写入 stderr；重定向后使用纯文本。安静模式不会隐藏 BuildKit 或子进程输出。

## 宿主机与目标平台

| 运行位置 | 支持范围与要求 |
| --- | --- |
| 原生 Linux x86_64/ARM64、Windows x86_64、macOS ARM64 | 在受支持的 Python 次版本上执行宿主机命令 |
| Docker Engine | 必须能运行 Linux `amd64` 解析容器 |
| Docker Buildx | 必须能构建 Linux `amd64` 镜像 |
| 生成的镜像 | 使用 CUDA 的 Linux `amd64`；见 [GPU 运行要求](runtime.zh-CN.md#gpu-宿主机要求) |

ARM Engine 需要 amd64 模拟支持，或改用 amd64 端点。仅选择远程 Buildx 构建器，不会提供解析阶段所需的 Engine 执行能力。Windows 通常使用 Linux 容器模式的 Docker Desktop，不支持 Windows 容器。

原生宿主机测试不证明 Docker Desktop 端到端构建或 SSH 代理转发可用。相关失败保留 Docker/BuildKit 诊断；已测试的平台矩阵见[测试手册](../../tests/README.md#platform-coverage)。

本地来源及祖先目录都应使用真实路径，符号链接、junction 和特殊文件会被拒绝。macOS 上使用 `/private/tmp`、`/private/var`，避免通常为符号链接的别名。不要让不可信进程在 cdh 读取期间修改所选输入。

## 选择锁定与检查模式

`host render` 支持下表全部模式。`host build` 支持默认、`--locked` 和 `--upgrade-lock`；检查和预览使用 render。

按需要选择模式：

| 需求 | 模式 | 结果 |
| --- | --- | --- |
| 修改配置后构建 | 默认 | 复用未变的锁定输入，解析新增或变更输入，移除已删除项，写入锁文件和构建目录 |
| 严格使用已准备且匹配的目录 | `--locked` | 要求锁定输入和目录匹配，不写入，也不向外部来源重新解析；`host build` 随后仍调用 Buildx |
| 更新浮动选择 | `--upgrade-lock` | 重新解析浮动选择，保留未变的精确选择，写入更新后的锁文件和目录 |
| 检查现有目录是否匹配 | `--check` | 按默认策略解析，比较完整预期结构和本地文件内容；不写入、不构建 |
| 预览准备结果 | `--dry-run` | 检查输入并输出计划和发布选项；不写入、不构建 |

`--locked` 和 `--upgrade-lock` 互斥。`--check` 不能与它们或 `--dry-run` 组合。预览可以选择任一锁定策略，并以预览替代比较或发布。

**不写入不等于离线。** 默认、检查和预览模式在锁文件缺少所需信息时，可能访问外部来源或使用 Docker。完整且匹配的锁文件可避免这些准备路径调用 Docker。只有 `--locked` 禁止在锁定检查阶段访问外部来源和 Docker；真正构建镜像仍是独立的 Docker 操作，也可能下载依赖。

对于本地内容，`--locked` 检查结构和已启用的内容摘要，不比较未锁定来源的字节。需要该比较时使用 `--check`。格式错误或不受支持的锁文件会导致失败，并提示删除后重新生成。

### 锁文件保留什么

锁文件记录所选的直接输入，例如解析后的版本、Git 提交和选择锁定的本地内容。直接软件包 URL 会被视为浮动来源，`--upgrade-lock` 会重新解析，即使 URL 文本中包含版本或提交号。对这类 URL 锁定的是要求及解析出的顶层包版本，不是下载内容或实际获取的 VCS 提交。

匹配的锁文件不保证离线构建、逐字节一致的镜像、完整的间接依赖锁定、确定的钩子和安装器效果，或部署时状态。最终 `/opt/cdh/build/manifest.json` 记录镜像构建后执行的检查，不是健康检查，也不是另一份构建输入。

## 本地内容与复制方式

文件放置规则见[配置指南](configuration.zh-CN.md#在构建时添加文件)。准备好的本地文件和节点独立于来源，所以构建时可以不再保留原目录。源文件修改后，应重新准备构建目录。

### 选择复制模式

通常保持 `[cdh].local_file_mode = "auto"` 即可；只有需要指定复制方式时才修改。此设置适用于本地文件、目录内容和本地自定义节点。

| 模式 | 什么时候使用 | 无法快速克隆时 |
| --- | --- | --- |
| `auto`（默认） | 日常构建：优先尝试写时复制克隆 | 改用普通复制 |
| `clone` | 必须使用写时复制克隆 | 报错，不发布构建目录 |
| `copy` | 始终复制文件内容 | 不尝试克隆 |

两种方式都不修改来源，并生成独立文件：之后修改来源不会改变已准备的构建目录。克隆可以减少复制写入和额外磁盘占用，但 Docker 仍须读取文件，远程构建器仍须接收这些内容。

在 macOS 上：

- 兼容卷支持快速克隆。跨卷复制时，`auto` 使用普通复制。
- 支持只读来源。带有禁止修改（immutable）或仅允许追加（append-only）标记的文件，在 `auto` 模式下使用普通复制，避免副本继承限制后无法正常处理或清理。
- 克隆保留下载标记（`quarantine`）、Finder 信息和资源叉等扩展属性。普通复制生成的元数据可能不同。

权限不足、磁盘已满或其他 I/O 故障会使操作停止，不会触发普通复制重试。

### 内容检查

| 选择 | 成本与保证 |
| --- | --- |
| `content_lock = false`（默认） | 普通规划检查结构，不散列源文件；后续复制、比较或 Docker 使用仍需读取内容 |
| `content_lock = true` | 完整读取并散列所选内容，准备目录时校验，并在最终镜像中校验构建文件内容 |
| `--check` | 流式比较来源与构建目录的字节，包括未锁定文件 |

散列和比较使用有界内存，但仍需读取完整文件。未锁定的预览或成功的快速克隆不证明每个数据块都可读，后续读取仍可能失败。不完整的准备结果不会被发布。

对于本地节点，锁定范围是所选输入（包括根 `.dockerignore`），不包括钩子和安装器修改后的安装目录。被排除的内容不影响摘要。见[本地节点选择](configuration.zh-CN.md#选择自定义节点和构建钩子)。

## 通过 HTTPS 访问私有 Git 节点

按[配置指南](configuration.zh-CN.md#提供凭据)声明规则和凭据来源。cdh 将其用于宿主机 Git 解析；只要存在直接 Git 节点，还会将生效 Git 规则引用的全部不同凭据提供给合并的节点构建步骤，供递归子模块等使用。

该步骤内的钩子和安装器属于可信代码，可以读取或泄露这些凭据。cdh 不把解析值和来源位置写入自己的持久构建产物或诊断，但不会替任意子进程输出脱敏。命令正常结束时会尝试清理临时凭据，突然终止不能保证清理完成。

轮换令牌不会使已缓存的 BuildKit 步骤失效，因此命中缓存不证明新凭据可用。需要重新认证时，使用常规 BuildKit 缓存控制，例如手工 Buildx 构建的 `--no-cache`。

## 通过 SSH 访问私有 Git 节点

先将身份加载到默认 SSH 代理，并把服务器主机密钥加入默认 known-hosts 文件，然后构建：

```bash
cdh host build -f cdh.toml -t my-comfy:dev --load --ssh
```

| 宿主机 | 代理与信任文件 |
| --- | --- |
| POSIX | 要求 `SSH_AUTH_SOCK`，转发默认代理和已有的默认用户级、系统级 known-hosts 文件 |
| Windows | Docker/BuildKit 选择默认代理；cdh 提供已有的用户 `~/.ssh/known_hosts` 和 `known_hosts2`，不提供系统级文件 |

cdh 不加载密钥、不添加主机密钥，也不复制 `~/.ssh/config`。应使用显式主机和端口，例如 `ssh://git@example.test:2222/group/node.git`，并准备匹配的 known-hosts 条目。不支持 SSH 别名、`ProxyJump`、自定义密钥或信任文件选择器，以及原始私钥文件。HTTPS 凭据单独配置。

没有直接 Git 节点时，`--ssh` 会警告并跳过转发。认证、主机密钥和子模块错误保留 Git/OpenSSH 诊断。节点步骤内的钩子和安装器可以使用代理并读取信任文件，私钥内容仍由代理持有。缓存可能跳过认证；验证访问变化时需绕过相应 BuildKit 缓存。

## 经过认证的 HTTPX 文件下载

配置 [HTTPX 规则](configuration.zh-CN.md#为-httpx-下载提供认证)。验证和准备目录只检查引用，不读取令牌。只要构建包含生效的 HTTPX 文件，`host build` 就会读取生效下载规则引用的全部不同凭据，并仅提供给文件下载步骤。

构建凭据不是运行时凭据。[运行时下载](runtime.zh-CN.md#认证下载与凭据轮换)需要独立配置容器内可见的来源。与 Git 凭据一样，轮换令牌不会使下载层缓存失效。

## 直接构建已准备的目录

公开来源的构建目录可直接使用：

```bash
docker buildx build --load -t my-comfy:dev .cdh/build/current
```

移动时复制**整个**目录，保留生成文件和元数据。修改生成的输入可能使校验失败。构建器可以读取这些输入，应保护目录和缓存的访问权限。

手工构建绕过 cdh 的宿主机凭据管理，由调用者负责提供 Dockerfile 声明的凭据挂载及其清理。例如，引用 `github_pat` 的 Git 规则使用：

```bash
docker buildx build \
  --secret "type=env,id=cdh-git-credential-github_pat,env=CDH_GITHUB_PAT" \
  --load -t my-comfy:dev .cdh/build/current
```

下载规则使用名为 `cdh-downloader-credential-<name>` 的文件挂载。不要把令牌作为构建参数，也不要放进构建目录。SSH 需要 `--ssh default` 和相应信任文件，例如在 POSIX shell 中添加 `--secret "type=file,id=cdh-ssh-known-hosts-user,src=$HOME/.ssh/known_hosts"`。其他受支持的挂载 ID 见 Dockerfile；来源不存在的信任文件挂载应省略。SSH 和 HTTPS 输入可以同时使用。

调试生成结果时，可查阅开发文档中的[构建目录布局](../dev/contracts.md#rendered-context-layout)。

## 钩子来源目录

构建钩子没有隐式来源目录，验证、准备目录和构建时应传入同一个根目录：

```bash
cdh host validate -f cdh.toml --build-hooks-dir build-hooks
cdh host build -f cdh.toml -t my-comfy:dev --load --build-hooks-dir build-hooks
```

只包含被引用的普通 `.sh` 和 `.py` 文件，并保留安全的相对路径。脚本源码会留在镜像及其层中，不要写入机密。见[构建钩子示例](../../examples/build-hooks/)。

在 render/build 中使用 `--runtime-hooks-dir runtime-hooks` 烘焙运行脚本。只选取 `pre-start.d`、`post-start.d` 和 `stop.d` 下的直接 `.sh`、`.py` 文件。其他普通条目会被忽略并警告；不安全条目和读取失败会报错。省略此项则不添加钩子。挂载的运行时钩子独立配置，见[运行时钩子](runtime.zh-CN.md#运行时钩子与启动顺序)。

## 镜像标签与外部缓存

`-t` 列表整体替换 `[build].tags`。标签部分可使用以下表达式：

| 表达式 | 值 |
| --- | --- |
| `${{ comfyui.release }}` | 不带 `v` 的已接受正式版本；所选身份没有正式版本时失败 |
| `${{ comfyui.commit }}` | 完整提交号 |
| `${{ comfyui.commit.prefix(n) }}` | 提交号前 12–40 个字符 |

不带标签的镜像名表示 `latest`，解析后的发布目标必须唯一。仅更改标签或加载/推送方式不会改变镜像内容或使已准备的目录失效。多标签推送不是事务操作，部分失败后应检查仓库状态。

用 `--cache-from` 和 `--cache-to` 接入构建器支持的缓存后端：

```bash
cdh host build -f cdh.toml -t registry.example.com/my-comfy:dev --push \
  --cache-from "type=registry,ref=registry.example.com/cache/my-comfy:build" \
  --cache-to "type=registry,ref=registry.example.com/cache/my-comfy:build,mode=max"
```

两个选项可单独使用。通过 Docker 或缓存后端的机制认证，不要把凭据嵌入选项值。缓存后端兼容性由 Docker 决定。

## Python 环境和软件源

ComfyUI 及应用依赖位于 `/opt/venv`。cdh、comfy-cli 和配置的 uv 工具分别位于 `/opt/uv/tools` 下，命令入口在 `/opt/uv/bin`。命令名冲突会失败，不会覆盖其他工具。

普通软件包和通用间接依赖使用 `[python].index_url`。PyTorch 组中来自索引的包使用 CUDA 对应的 PyTorch 索引，不回退到普通索引。允许的非核心直接来源保留原地址；受保护的基础包不能用这种方式替换。

直接来源安装会验证解析出的顶层包名和版本，不验证下载产物身份。可信节点安装器引入的依赖和修改不在完整锁定保证内。

### 软件包编译环境

选择包含所需编译器、头文件和库的基础镜像。提供编译器设置不会安装缺少的开发工具。

软件包安装会接收受支持的代理设置，以及基础镜像和 `[system.env]` 中经过限制的工具链值。不必重复基础镜像已有的值。任意 `PATH`、pip、uv 或 Python 配置不能覆盖 cdh 管理的解释器、软件源和约束。修改实现时参阅[软件源契约](../dev/contracts.md#python-and-pytorch-package-source-ownership)。
