# 构建和锁定镜像

[English](build-and-lock.md) | 简体中文

本指南介绍本地验证、canonical lock 协调、渲染构建上下文以及 Docker 镜像构建。请先阅读[配置指南](configuration.zh-CN.md)，以选择和分层配置文件。以下命令假定你的配置名为 `cdh.toml`，并从其所在目录运行。

多行命令使用 POSIX shell 的续行语法。在 Windows 上，请将命令写在一行，或者把每行末尾的 `\` 换成 PowerShell 的反引号续行符。

## 宿主机与目标平台

所有 `cdh host *` 工作流都能在受支持的 Windows 和 Linux 宿主机上原生运行。请使用项目 README 中常规的 [`uv tool` 或 `pip` 命令](../../README.zh-CN.md#安装)来安装 cdh；安装程序会选择所需的平台依赖。Docker 构建始终以 Linux `amd64` 为目标。Windows 宿主机通常使用运行 Linux 容器并支持 Buildx 的 Docker Desktop；其他 endpoint 必须提供等效的 Linux `amd64` Buildx 行为。cdh 不构建或运行 Windows 容器。`cdh container *` 用于在生成的 Linux 镜像内执行。

Windows 自动化验证覆盖原生 CLI、文件系统、Git、渲染、打包以及 Docker/Buildx 适配器行为，但不会运行真实的 Docker Desktop 构建，也不证明 Docker Desktop 的 SSH agent forwarding。因此，Docker Desktop、builder 或 agent 集成失败会保留其底层 Docker/BuildKit 诊断。

读取本地 Secret、Hook 和构建文件输入时，cdh 会验证当时观测到的文件类型和词法路径形状，并拒绝已观测到的符号链接、Windows junction 或其他 reparse point，以及特殊文件。Secret 文件还会执行 65,525 字节上限。Hook 文件和启用内容锁的本地构建文件会把流式读取的来源字节绑定到 digest，并在发布前复验该 digest；未启用内容锁的本地构建文件仍会完成准入和 materialization，但不会创建 cdh 内容 digest。这并不隔离其他本地进程：不要允许不受信任的进程在 cdh 运行期间并发修改选中的输入文件或其目录。

## 验证、渲染和构建

在解析或构建任何内容之前验证配置：

```bash
cdh host validate -f cdh.toml
```

验证在本地完成：它不会调用解析提供方或 Docker，也不会写入文件。可以重复使用 `-f/--file` 来指定配置层；cdh 按命令行中的顺序合并这些配置层，并验证最终生效的结果。

渲染可复用的构建上下文和 canonical lock：

```bash
cdh host render \
  -f cdh.toml \
  -o .cdh/build/current \
  --overwrite
```

渲染会复用匹配的 lock。当必须解析缺失或已变化的镜像身份时，它可能会使用 Docker。`--overwrite` 只会替换现有且有效的 cdh 管理上下文；否则 cdh 会拒绝替换。

cdh 会先准备好完整的替代上下文，再更改现有上下文。`--overwrite` 不保证崩溃恢复：进程或宿主机中断可能导致输出缺失，而先前的完整上下文仍保留在同级备份中。如果诊断给出了保留备份的路径，请保留该备份，以便在重试前进行人工恢复。

使用 Docker Buildx 构建镜像：

```bash
cdh host build \
  -f cdh.toml \
  --context-dir .cdh/build/current \
  -t my-comfy:dev \
  --load
```

`host build` 会渲染所选上下文，然后调用 Buildx。请提供一个或多个 `-t/--tag` 值，或者配置 `[build].tags`。使用 `--load` 将镜像载入本地 Docker 镜像存储，或使用 `--push` 推送至 registry；这两个选项互斥。

### 动态发布 tag

发布 tag 可以通过且仅能通过以下表达式引用已接受的 ComfyUI 身份：

- `${{ comfyui.release }}` 是不带前导 `v` 的规范化正式 release；
- `${{ comfyui.commit }}` 是完整的 40 字符 commit；以及
- `${{ comfyui.commit.prefix(n) }}` 使用 `n` 个 commit 字符，其中 `n` 的范围是 12 到 40。

表达式只能出现在显式 tag component 中。不带 tag 的字面量镜像名称仍表示 `latest`。当已接受身份没有正式 release 时（包括 nightly 和直接 commit 构建），引用 `comfyui.release` 会产生 hard error；cdh 既不会跳过该 tag，也不会提供 fallback。`-t/--tag` 列表会整体替换 `[build].tags`，并接受相同语法。解析后的有序 target 在经过 Docker familiar name 规范化后仍必须唯一。

Tag 和 `[build].output` 是进程内 publication choice。它们不会进入 canonical lock、BuildPlan、渲染上下文、最终 manifest、镜像配置 digest 或镜像内容身份。因此，只改变这些 choice 可以复用同一渲染上下文和镜像构建。Registry 发布不是事务操作：如果多 tag `--push` 中途失败，请检查 registry 并重试缺失的 target。

### 复用外部构建缓存

使用 `--cache-from` 复用已有的 BuildKit 缓存，使用 `--cache-to` 保存本次构建生成的缓存：

```bash
cdh host build \
  -f cdh.toml \
  --context-dir .cdh/build/current \
  -t registry.example.com/my-comfy:dev \
  --push \
  --cache-from "type=registry,ref=registry.example.com/cache/my-comfy:build" \
  --cache-to "type=registry,ref=registry.example.com/cache/my-comfy:build,mode=max"
```

每个选项接受一个 Docker Buildx 缓存参数，也可以单独使用。请选择当前 Buildx builder 支持的缓存后端，并通过 Docker 或缓存后端支持的凭据机制完成认证，不要把凭据放入选项值中。请参阅 Docker 的[缓存后端文档](https://docs.docker.com/build/cache/backends/)。

## 通过 HTTPS 访问私有 Git 自定义节点

请按照[提供私有 HTTP(S) Git 凭据](configuration.zh-CN.md#提供私有-https-git-凭据)配置 Secret source 和 `[[cdh.git.credentials]]` route。在一条宿主机命令期间，cdh 会使用选中的 route 解析 direct-Git 身份，并让 BuildKit 可使用生效 route 的凭据来安装自定义节点和递归 submodule。Token 不会被放入 Git URL 或命令参数。

Secret 会在命令范围内惰性处理。cdh 会让 source locator 和解析后的值避开持久构建工件及其自身诊断，并在命令通过受支持的成功、错误或中断路径退出时尝试清理。普通清理失败会被报告，但进程或宿主机突然终止时无法保证完成清理。这是结构性非持久化边界，而不是沙箱：受信任的自定义节点 Hook 和安装程序仍可以读取、输出或复制其合并构建步骤可用的凭据。`http://` credential route 可以使用，但会因缺少 TLS 传输保密性而发出 warning。

在 POSIX 上，环境变量 Secret 会保留环境值的原始字节；在 Windows 上，cdh 会将 Unicode 环境值编码为 UTF-8。文件 Secret 仍必须是常规文件，且上限为 65,525 字节。当 POSIX 的 group 或 world 权限位已设置时，cdh 会发出 warning。在 Windows 上，请自行限制源文件的 ACL，因为 cdh 不实现通用的 Windows access audit。由 cdh 管理的临时 Secret 快照仍会通过 POSIX mode 或受保护的 Windows DACL 保持私有。

BuildKit 不会把 Secret 内容纳入 `RUN` 指令的 cache key；只有 Secret ID 和 mount 属性参与。轮换 token 后，已经完成的自定义节点层仍可能被复用，而不访问当前 credential source。直接构建渲染上下文时，如需重新检查鉴权，请使用 Buildx `--no-cache` 或其他常规 BuildKit cache 控制。cdh 刻意不会把 token hash 作为 cachebuster。

### 直接构建渲染后的 HTTPS 上下文

渲染后的 Dockerfile 会为 direct-Git 构建可用的每个 credential 声明稳定且 required 的 Secret ID。自行调用 Buildx 时，必须把所有声明的 ID 绑定到对应值。例如，复制或取消注释 [`examples/full.toml`](../../examples/full.toml) 中两个完整的私有 HTTPS 配置块，再渲染该配置，就会生成以下 ID：

```bash
docker buildx build \
  --secret "type=env,id=cdh-git-credential-github_pat,env=CDH_GITHUB_PAT" \
  --secret "type=file,id=cdh-git-credential-gitlab_pat,src=/path/to/gitlab-pat" \
  --load \
  -t my-comfy:dev \
  .cdh/build/current
```

手动调用方负责 source 接纳和清理。Credential Secret、SSH forwarding 和 known-hosts Secret 是彼此独立的输入，可以同时提供。

## 通过 SSH 访问私有 Git 自定义节点

当 direct-Git 自定义节点或其递归 submodule 需要宿主机上的 SSH 身份时，使用 `--ssh`。构建前，请将所需身份加载到默认 SSH agent，并将服务器主机密钥加入默认的 OpenSSH known-hosts 文件。

```bash
cdh host build \
  -f cdh.toml \
  --context-dir .cdh/build/current \
  -t my-comfy:dev \
  --load \
  --ssh
```

在 POSIX 上，该选项要求 `SSH_AUTH_SOCK` 非空；cdh 会转发该默认 agent，以及现有的默认用户和系统 known-hosts 文件。在 Windows 上，`SSH_AUTH_SOCK` 不是 cdh 的前置条件：cdh 会请求 BuildKit 的 `--ssh default`，由 Docker/BuildKit 选择并验证 agent，同时只自动提供现有的用户级 `~/.ssh/known_hosts` 和 `~/.ssh/known_hosts2` 文件。Windows 不支持自动发现系统级 known-hosts。无论在哪个平台，cdh 都不会检查 agent、添加主机密钥、复制私钥或读取 `~/.ssh/config`。对于自托管服务或非默认端口，请使用 `ssh://git@example.test:2222/group/node.git` 之类的显式 locator，并将对应的主机及端口条目加入受支持的默认 known-hosts 文件。该选项不支持 SSH alias、`ProxyJump`、自定义密钥或信任文件 selector 以及原始密钥文件。`--ssh` 不会提供 HTTPS token；请通过 `cdh.git.credentials` 配置它们。

如果生效配置没有 direct-Git 节点，`--ssh` 会输出一次 warning，并在不转发任何内容的情况下继续。否则，POSIX 上缺少 agent 会在解析源码前失败。在 Windows 上，默认 agent forwarding 不可用时由 Docker/BuildKit 报告；主机密钥、鉴权以及 Git/submodule 的失败同样会保留其底层诊断。

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
  -f cdh.toml \
  --build-hooks-dir build-hooks
```

配置中的路径相对于此目录。cdh 只接纳被引用的常规 `.sh` 和 `.py` 文件，并保留它们安全的相对布局。构建 Hook 是受信任代码，其经过验证的源字节会保留在最终镜像及其镜像层中。请勿在其中放入机密信息。参见[构建 Hook 示例](../../examples/build-hooks/)。

向 `render` 或 `build` 传入 `--runtime-hooks-dir`，以烘焙 runtime hook 目录树：

```bash
cdh host render \
  -f cdh.toml \
  -o .cdh/build/current \
  --build-hooks-dir build-hooks \
  --runtime-hooks-dir runtime-hooks \
  --overwrite
```

只有直接位于 `pre-start.d/`、`post-start.d/` 和 `stop.d/` 下的普通 `.sh` 和 `.py` 文件会被选择并固化。其他普通文件和目录会在不递归遍历的情况下被忽略并产生聚合警告；不安全的文件系统条目以及来源检查/读取失败仍是错误。省略该选项时不会烘焙运行时 Hook 目录树。挂载的运行时 Hook 是独立的部署时输入；参见[运行时指南](runtime.zh-CN.md)和[运行时 Hook 示例](../../examples/runtime-hooks/)。

## 构建文件与本地上下文 materialization

构建 `[[files]]` 声明是最终镜像内容的权威。HTTP 文件会下载到 staging，并在通过已配置的 checksum 后原子替换目标。本地文件会 materialize 到 Plan 拥有的 `build/files/` 上下文 slot，再通过 `COPY --link --chmod=0644` 放到精确目标。lower image 中已有的内容不会阻止这两种操作，因此构建文件没有 `overwrite` 设置。每个目标仍必须是 `COMFYUI_PATH` 的严格后代路径。

通过 `[cdh].local_file_mode` 选择本地字节如何进入渲染上下文：

- `auto` 是默认值。它会尝试 copy-on-write clone，只有在 clone capability 不可用或文件系统不支持该操作时才回退到流式 copy。
- `clone` 要求支持 copy-on-write clone；不可用时会失败且不发布上下文。
- `copy` 始终执行固定 buffer 的流式复制。

任何 clone mode 都不会使用 hardlink 或 symlink：已发布的上下文文件不受后续来源变更影响。可用的本地文件系统上，clone 可以避免实际复制未变化的 extent，但完整文件仍属于上下文。BuildKit 必须读取它，远程 builder 也必须接收它，因此 `content_lock = false` 不会消除上下文存储、builder cache 或上传成本。

使用 `content_lock = false` 时，普通规划不会对来源执行 hash。显式 `--check` 会先比较安全文件形状和 size，仅在 size 相同时才以流式方式逐字节比较。使用 `content_lock = true` 时，规划会流式计算 SHA-256 并写入 canonical lock 和 BuildPlan；materialization 会重新 hash 来源，而 `--check` 会以流式方式将上下文 slot 与该预期 digest 比较。这些操作均为有界内存，但当结果需要时必然读取完整文件。

只有 HTTP 构建文件会投影到 `runtime/config.toml`。本地来源 locator 仅属于宿主机，不会成为 runtime import 指令；部署时替换仍由挂载的运行时配置独立负责。

## 经过认证的 HTTPX 文件下载

Downloader credential route 只授权 cdh 的 HTTPX 文件下载指令。`validate` 与 `render` 会检查 route 结构及 Secret reference，但不读取 Secret 值。构建中只要存在至少一个生效的 HTTPX 文件，`host build` 就会在启动 Buildx 前解析 effective downloader route 引用的完整 distinct Secret 集，并只将其授予文件下载指令。因此 redirect 可以选择另一条已声明 route，而 installer、Git、Hook 及其他构建指令都不会获得 downloader credential。

Route pattern 与逻辑 Secret 名称属于普通构建 metadata。Secret source locator、解析后的 token 值、生成的 authorization header 及 token digest 不会持久化到 cdh 管理的 lock、BuildPlan、context metadata、manifest、镜像 metadata/history 或命令输出。构建 route 和 Secret definition 不会固化到运行时配置；部署时下载需要认证时，应独立声明 runtime route 和容器内可见的 Secret source。

手动构建已渲染 context 会绕过宿主机 Secret session。请按渲染 Dockerfile 中显示的稳定 `cdh-downloader-credential-<name>` ID 提供每个必要的 file-backed mount。不要通过 build argument 传入 token，也不要把 token 放进 context。

BuildKit Secret 内容通常不会使指令 cache 失效。因此 token 轮换仍可能复用已经完成的下载 layer，cache hit 也不能证明当前 credential 有效。构建必须重新执行认证请求时，请使用普通 BuildKit cache control；cdh 不会加入由 token 派生的 cachebuster。

## 从生效配置到上下文

cdh 使用单向前进的规划流程：

```text
effective configuration -> canonical lock -> BuildPlan -> rendered context
```

生效配置描述意图。`config.lock.toml` 记录宿主机协调所使用并已接受的精确外部 identity、Hook identity 和显式启用内容锁的本地文件 identity。随后，cdh 构造一个不可变的 BuildPlan，作为构建时执行权威。上下文渲染会将该计划连同其精确 wheel 和已准入的本地输入一起投影出来；构建时辅助程序不会重新读取宿主机配置或 lock 来作出新的规划决策。

## 协调模式

解析提供方策略与文件系统/构建副作用彼此独立。可以从以下五种面向用户的模式中选择：

| 模式 | 解析行为 | 上下文和构建行为 |
| --- | --- | --- |
| 默认 | 复用未变更的条目，解析缺失或已变更的输入，并移除已删除的身份。 | 写入已接受的 lock 和渲染上下文。 |
| `--locked` | 要求现有 lock 与启用内容锁的本地输入完全匹配；协调期间不调用解析提供方或 Docker。 | 比较现有上下文且不写入任何内容。未启用内容锁的本地 source bytes 不会被比较；如需显式流式比较，请使用 `--check`。检查通过后，`host build` 仍会调用 Buildx。 |
| `--upgrade-lock` | 刷新浮动选择器，同时保留未变更的精确选择。 | 写入更新后的 lock 和渲染上下文。 |
| `--check` | 应用默认协调策略。 | 将完整的预期上下文与现有上下文进行比较；不写入任何内容，也不构建。 |
| `--dry-run` | 使用默认策略；与 `--locked` 或 `--upgrade-lock` 组合使用时除外。 | 输出精确的 BuildPlan，并在独立的进程内 `Buildx output` 区段中显示适用时已展开的 mode 和 tag，否则显示 `None`；不写入任何内容，也不构建。 |

`--check` 不能与 lock 策略或 dry-run 修饰选项组合使用。`--locked` 与 `--upgrade-lock` 互斥。当 `--dry-run` 与 lock 策略组合使用时，预览行为会取代上下文比较或发布。`Buildx output: None` 表示本次调用没有 publication output plan；它既不属于 BuildPlan，也不是 BuildPlan 中缺少的字段。

不写入并不一定意味着离线。默认、`--check` 和 `--dry-run` 可能会调用解析提供方；当当前 lock 无法提供所需的镜像身份时，它们可能还需要 Docker。完整且匹配的 lock 可使这些路径无需 Docker。只有 `--locked` 禁止在协调期间调用解析提供方和 Docker；Docker Buildx 仍是 `host build` 的一项独立要求。

每个在目标环境生效的具名 Python direct reference 都会被视为 moving，即使其 URL 文本包含版本、hash fragment 或 VCS ref。默认协调和 `--check` 可以复用未变且匹配的结果，而 `--locked` 要求存在匹配结果，并且在宿主端协调期间不接触 source。`--upgrade-lock` 会重新解析每个 moving direct reference。这些模式都不会把 URL 或 VCS ref 变成 artifact lock；之后执行的 Buildx build 仍可能需要获取并安装每个生效的 direct source。

格式错误或不受支持的 lock 文件会以失败关闭，并给出诊断信息，提示删除并重新生成 lock。

## 渲染上下文

渲染上下文包含：

- `.cdh-rendered`，cdh 管理上下文的宿主机标记；
- `config.lock.toml`，仅供宿主机使用的协调状态；
- `build-plan.json`，规范的构建时执行计划，仅在每个所属构建指令运行期间以只读方式挂载；
- `bootstrap/comfyui_docker_helper-<version>-py3-none-any.whl`，安装到镜像中且经过精确验证的 cdh wheel；
- `build/hooks/`，配置后仅包含被引用且经过验证的构建 Hook 字节；
- `build/files/`，包含按 Plan 定址且独立复制或克隆的宿主机本地构建文件；
- `runtime/config.toml`，派生自 BuildPlan；
- `runtime/hooks/`，配置后包含经过验证且已烘焙的运行时 Hook 目录树；
- `Dockerfile`，使用字面量且带 digest 的基础镜像引用渲染而成；以及
- `.dockerignore`，将 `config.lock.toml` 和 `.cdh-rendered` 排除在 Buildx 输入之外。

上下文不包含根级 `config.toml`。宿主机本地源路径、Secret source locator、解析后的 Secret 值、发布 tag 及 output selector 都不是 BuildPlan 输入。Dockerfile 没有能够替换由 lock 定权的镜像身份的参数。完整 Plan 仍保留在宿主机上下文中，所选的本地或远程 builder 仍可访问它，但每条指令的只读挂载不会把 `/opt/cdh/build/build-plan.json` 持久化到最终镜像中；最终 manifest 仍保留 Plan digest 绑定。

## Python 环境和包源

镜像将应用软件包和用户工具保持在彼此独立的所有权域中：

- `/opt/venv` 包含 ComfyUI、其应用依赖项，以及可选且由检出版本拥有的 Manager/`cm-cli` 功能。
- cdh、可选的 `comfy-cli`，以及每个配置的 `[python].uv_tools` 软件包，分别使用 `/opt/uv/tools` 下的独立环境。
- 工具命令链接在 `/opt/uv/bin` 下；发生可执行命令所有权冲突时会失败，而不是替换现有命令。

`comfy-cli` 是可选的用户工具，在镜像构建期间不会用于安装 ComfyUI、Manager 或 Registry 自定义节点。

除属于 PyTorch 分组的软件包外，由 cdh 控制的 index 解析和普通传递依赖都使用 `[python].index_url`。在 PyTorch 分组中，来自所选 ComfyUI 检出版本的受保护 requirement 及每个 index-backed 成员只使用由 CUDA 派生的 PyTorch index；缺失的 index-backed 成员不会回退到普通 index 上的同名包。在目标环境生效、非受保护且由用户配置的 `name @ URL` 成员则保留其编写的 direct source，并且不会获得 PyTorch-index route。该分组会被原子地解析和安装，最终顶层版本会得到精确验证，受保护的基础包也不能被 direct reference 替换。

`python.extra_packages` 中每个在目标环境生效的 direct requirement 都会保留至应用安装，同时 `[python].index_url` 仍用于 index-backed 和传递依赖。每个生效的 `python.uv_tools` requirement 都使用 managed Python interpreter 安装在独立的 `/opt/uv/tools/<name>` 环境中；direct tool 保留其编写的 source，传递依赖仍使用默认 Python index。安装过程不会增加 downloader、URL rewrite 或第二条软件包路径。

安装自定义节点时，Registry Manager 以及 Direct-Git 用于根 requirements 和 `install.py` 的 Python 安装进程都会获得由 BuildPlan 定权的普通 index、由 CUDA 派生的 PyTorch index，以及通过运行时约束和隔离构建约束渠道提供的同一份精确应用约束。这样，隔离的软件包构建可以根据受保护的应用基础完成解析，但受信任安装程序选中的依赖并不会因此全部成为 cdh lock 或 BuildPlan 输入。Manager 负责 Registry 节点特有的安装效果；对于 Direct-Git 节点，cdh 会验证精确的根 commit 和递归 gitlink 并接纳根 requirements，而依赖安装和 `install.py` 效果仍属于受信任代码执行。

cdh 会记录并验证解析得到的精确顶层软件包版本，但不会锁定 direct source 背后的字节或 VCS commit。镜像构建会安装配置中指定的 source；如果最终软件包名称或版本与解析结果不匹配，构建会失败。

软件包 direct reference 是普通公共配置，而不是 Secret locator。生效的 reference 会成为渲染后的构建输入，并可能对 builder 或 build cache 可见；配置为 direct 的 uv-tool reference 还可能出现在 image history 中。URL userinfo 会被拒绝，cdh 也不会把 downloader/Git credential route 附加到软件包安装。切勿在这些 reference 中放入 token 或私有凭据。

## 最终证据和重放边界

所有镜像变更成功后，cdh 会写入严格的最终状态观测 `/opt/cdh/build/manifest.json`。它绑定镜像配置、canonical lock 和 BuildPlan 的 digest，并记录预期和观测到的直接身份。该 manifest 是证据，而不是另一个解析器、lock、重放输入、支持性结论或一般性的服务健康检查。

cdh 为由 cdh 控制的直接输入提供有界且经过验证的重放。对于在目标环境生效的软件包 direct reference，重放 identity 是用户编写的 request 加精确的已安装顶层分发包版本，而不是已获取 artifact：它不会证明某个 URL 的内容未变，也不会把 moving VCS ref 固定到观测到的 commit。Registry Manager 或 Direct-Git 安装脚本选中的 moving direct/VCS 依赖同样不会被 cdh 独立锁定，也不会获得 cdh 的来源证明。`--locked` 只在宿主端协调期间避免接触 source；之后执行的 Buildx build 仍可能需要获取并安装该生效且由用户编写的 source。这并不承诺离线构建或字节完全一致的构建，也不承诺对传递依赖项或每个已获取 artifact 进行完整锁定，不为缺少用户所提供 hash/checksum 的软件包或文件下载提供真实性保证，不保证受信任安装程序或 Hook 的效果具有确定性，也不承诺重放部署时变更。
