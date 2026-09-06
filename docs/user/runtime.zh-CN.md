# 运行时与生命周期

[English](runtime.md) | 简体中文

本指南面向运行由 cdh 构建的镜像的用户。它说明了哪些设置无需重新构建镜像即可更改、如何从容器内控制正在运行的 ComfyUI 生命周期、运行时下载与 Hook 何时运行、如何启用可选的 SSH 访问，以及容器停止时会发生什么。

如需查看带完整注释的主机配置，请参阅 [`examples/full.toml`](../../examples/full.toml)。[配置指南](configuration.zh-CN.md)说明主机配置和分层；[构建与锁定指南](build-and-lock.zh-CN.md)说明主机端的选择如何成为固化到镜像中的输入。

## GPU 宿主机要求

使用 GPU 运行由 cdh 构建的镜像需要 NVIDIA Container Toolkit、版本不低于 `580.65.06` 的 NVIDIA 驱动，以及 Turing 或更新架构的 NVIDIA GPU。

## 设置容器时区

启动容器时可设置标准进程时区：

```bash
docker run --env TZ=Asia/Shanghai IMAGE
```

对于遵循标准 `TZ` 行为的程序，容器启动时的 `TZ` 会覆盖通过 `[system.env]` 固化的值。

## 运行时配置优先级

每个镜像都在 `/opt/cdh/runtime/config.toml` 中包含生成的运行时默认配置。你可以挂载可选的 `/etc/cdh/runtime/config.toml`，无需重新构建镜像即可更改仅限运行时的行为。

cdh 按以下顺序应用运行时设置，越靠后的来源优先级越高：

```text
built-in defaults < baked config < mounted config < environment
```

运行时配置包括 ComfyUI 的 `listen`、`port` 和 `extra_args`；cdh 下载与日志记录设置、downloader credential；运行时 Secret source；`system.ssh`；以及 `files`。运行时 TOML 文件中已知的仅限主机端字段会被忽略并产生警告。未知或其他不受支持的运行时字段会导致启动失败，而不会被静默接受。挂载的运行时文件无法安装软件包、更改选定的 ComfyUI 检出版本，也无法重新构建镜像。

每个 TOML 来源会先完成解析和运行时适用性检查。剩余的受支持值随后与默认值及环境覆盖合并，最后由 cdh 校验生成的生效运行时文档。因此，靠后的局部条目可以从靠前层继承省略的字段，但无效的最终结果仍会带来源上下文使启动失败。

普通运行时数组采用整列表替换：省略会继承靠前列表，靠后的非空列表会完整替换它，靠后的空列表会将其清空。这适用于 `comfyui.extra_args` 和 TOML 中的 `system.ssh.pub_keys`。`SSH_PUB_KEY` 是特意保留的追加例外。其他层合并后，cdh 会按声明的密钥类型与 base64 密钥 blob 对生效公钥进行稳定去重，并保留先出现的完整规范化行及其可选注释。因此，当 `SSH_PUB_KEY` 的密钥身份已存在时，即使注释不同也会静默地不做任何更改；否则 cdh 会追加其规范化行。

Downloader credential route 则按 canonical `match` 合并：靠后等价 route 会原子替换完整的靠前 route，新 route 会追加，`credentials = []` 会清空 catalog。每个 `[secrets.<name>]` source 都是独立的原子定义。运行时 route 与 source 由部署拥有，绝不会从构建时 counterpart 继承。

日志记录的环境变量覆盖项与 `[cdh.logs]` 一起列于[完整示例](../../examples/full.toml)。其他运行时环境变量覆盖项如下：

- `CDH_COMFYUI_LISTEN`、`CDH_COMFYUI_PORT` 和 `CDH_COMFYUI_EXTRA_ARGS`；
- `CDH_DEFAULT_DOWNLOADER`、`CDH_DEFAULT_DOWNLOAD_MODE`、`CDH_DOWNLOAD_MAX_ATTEMPTS`、`CDH_DOWNLOAD_FAILURE_POLICY` 和 `CDH_SHUTDOWN_TIMEOUT`；以及
- `SSH_ENABLE`、`SSH_PORT`、`SSH_PASSWORD` 和 `SSH_PUB_KEY`。

`CDH_COMFYUI_EXTRA_ARGS` 使用 POSIX shell 风格的单词解析，但不会执行 shell。运行时 TOML 和环境变量都不能在 `extra_args` 中放入 `--listen`、`--port`、`--auto-launch` 或 `--disable-auto-launch`；这些容器启动控制项由 cdh 负责。

环境变量覆盖和挂载的运行时输入属于部署时变更。它们不在固化镜像经过验证的重放边界之内。

## 运行时输出详细度与输出流

运行时详细度选项属于根命令，因此应写在 `container` 之前：

```bash
cdh -q container runtime COMMAND
cdh -v container runtime COMMAND
cdh -vv container runtime COMMAND
```

普通 `runtime serve` 会在 stderr 持续输出纯文本日志；在相应阶段存在时，它会显示初次启动或重启的生命周期、运行时文件准备、Hook、SSH、ComfyUI 启动与就绪等待，以及清理。即使 stderr 是终端，日志仍保持纯文本和完整行。`-q/--quiet` 会隐藏由 cdh 生成的信息性生命周期与进度行；`-v/--verbose` 增加数量、耗时和操作上下文，`-vv` 增加调试细节。quiet 与 verbose 不能组合使用；即使启用 quiet，警告和受控错误仍会显示。

运行时下载会标识配置的目标，以及它在当前批次和尝试序列中的位置。它会报告已传输字节；有可比较的总量时还会显示百分比、速率和预计时间，总量未知时则只显示字节信息，而不会虚构百分比。重试、停滞、恢复、文件就绪和队列结果都会以完整文本行输出。当运行时输出严重积压时，cdh 可能合并重复的进度更新；若省略了信息性更新，则会输出警告，而传输和 SSH 工作会继续。

ComfyUI、Hook 与 SSH 子进程的 stdout 和 stderr 仍是原始子进程输出。cdh 不会给这些字节添加前缀、重新设置样式、过滤或脱敏。容器原始 stdout 与 stderr 仍是主要日志流。根级详细度选项同样不会改变 `runtime status` 必需的人类或 JSON 结果、`runtime restart` 的结果，或 `runtime logs` 返回的合并字节。logs 客户端不会改变正在运行的控制进程的详细度设置。

## 运行时控制

请对需要控制的容器运行以下命令：

```bash
docker exec CONTAINER cdh container runtime restart
docker exec CONTAINER cdh container runtime status
docker exec CONTAINER cdh container runtime status --json
docker exec CONTAINER cdh container runtime logs --follow
```

只要部署没有覆盖 `PATH`，SSH 会话就会使用镜像的正常工具路径，因此可以直接按名称调用 `cdh` 和 `uv`。如果部署需要覆盖 `PATH`，则须保留 `/opt/uv/bin`，才能继续按名称调用 cdh、uv 和配置的 uv 工具。

`restart` 会在 cdh 停止当前 ComfyUI 运行时并重新启动的过程中保持等待。restart 被接纳后，会重新读取固化和挂载的运行时配置与 Hook，然后执行下文的正常启动顺序。重新启动的运行时仍使用容器启动时的环境；仅提供给 `docker exec` 命令的环境变量不会成为运行时覆盖项。同一时间只能执行一次 restart，因此并发请求会以 busy 错误退出。

没有 post-start Hook 时，ComfyUI 成功启动进程即表示 restart 成功。存在 post-start Hook 时，只有在条件式 readiness 成功且所有 post-start Hook 执行完成后，restart 才成功。异步下载只需被接收到队列中；restart 不会等待所有异步传输完成。

在 cdh 接纳 restart 前中断命令会取消该请求。接纳后，中断只会停止本地等待；restart 会在容器中继续，而 `status` 会显示其当前状态。客户端已知接纳的 operation ID 时，按 `Ctrl-C` 也会显示该 ID。restart 失败会报告给等待中的客户端，并在清理完成后使容器以非零状态退出。cdh 不提供 runtime `start` 或 `stop` 命令，`restart` 也没有 detach 或 no-wait 模式。ComfyUI 自然退出仍会结束容器。

`status` 显示当前 ComfyUI 运行时以及正在进行的 restart；`--json` 输出稳定的机器可读状态。这是当前的内存状态，不是健康检查或持久历史。

`logs` 可读取保留的输出，并在手动 restart 期间持续跟随。示例与保留范围见[查看和保留日志](#查看和保留日志)。控制进程必须仍在运行；控制端点缺失时，客户端不会启动本地运行时，也不会切换为直接读取文件。

请使用容器默认用户运行这些命令。不同 UID（包括通过 `docker exec --user` 选择的用户）无法访问运行时控制。

## 查看和保留日志

在容器内使用 `cdh container runtime logs`；从宿主机使用时，在相同命令前加上 `docker exec CONTAINER`。命令返回已捕获的 cdh 生命周期输出，以及继承运行时 stdout/stderr 的 ComfyUI、Hook 或服务输出。它不会收集任意日志文件、Docker 构建输出或独立的 exec/SSH 命令会话。

```bash
# 读取全部保留的输出后退出。
cdh container runtime logs

# 读取最近 200 行，或读取后继续跟随。
cdh container runtime logs --tail 200
cdh container runtime logs --tail 200 --follow

# 只跟随新输出。
cdh container runtime logs --tail 0 --follow

# 导出合并输出；诊断仍输出到终端的 stderr。
cdh container runtime logs > runtime.log
```

`--tail` 也接受 `all`；`-n` 和 `-f` 分别是 `--tail` 和 `--follow` 的短选项。不指定 tail 限制的 `logs --follow` 会先返回全部保留历史。

查询内容按 cdh 观察到的顺序将两个来源流合并到 stdout。警告和错误输出到 stderr；原始容器 stdout/stderr 仍然分开，并可由 Docker 日志后端读取。可直接阅读的 `runtime.log` 和编号归档文件保留原始输出，不添加时间戳、输出流标签或换行。轮转可能将一行分到两个文件中，重启后的追加也可能接续上一条未结束的行。字节保留与行选择的细节见[日志存储与查询契约](../dev/contracts.md#retained-logs-are-bounded-optional-copies)。

通过镜像配置或挂载的运行时 TOML 中的 `[cdh.logs]` 选择记录行为。精确字段、容量单位和容器启动时的环境变量覆盖见[完整示例](../../examples/full.toml)。

| 模式 | 可读取的历史 |
| --- | --- |
| `file`（默认） | 保留的轮转文件，加上当前内存中的末尾输出；重复字节只输出一次。 |
| `memory` | 当前控制进程保留的有容量上限的近期输出；不读取之前的文件。 |
| `none` | 不记录新历史。查询可只读已有的 cdh 日志文件，不创建、轮转或删除文件，并提示记录已关闭；存储不存在时以空结果成功结束。 |

每种模式都能只查看新输出。在 `none` 模式下，历史加实时跟随会明确警告：主动关闭记录期间的输出无法恢复。关闭记录不会关闭原始容器输出，也不会删除已有历史。

默认目录是 `/var/log/cdh`。`max_size` 分别限制内存与每个文件中保留的日志容量；增大它也会提高内存额度。`max_files` 包含活动文件。默认最多在内存中保留 20 MiB 日志，在文件中保留 100 MiB 日志。内存按需增长，进程在保留日志之外还有额外开销。

日志设置在控制进程的整个生命周期内固定。修改为有效的新值后执行 `runtime restart` 会产生提示，并继续使用原来的记录设置，直到容器重启。无效的生效日志值仍会使 restart 准入失败，并可能导致容器退出。等价容量写法不算配置变化。完整容器重启会使用新选定的目录，不会搬迁或删除旧目录中的日志。仅传给 logs 客户端的环境变量不会决定存储位置。

### 持久化文件与准备目录

内存历史可跨手动 ComfyUI 重启保留，但控制进程或容器重启后就会丢失。只要容器文件系统仍可用，已写入的文件就会在同一容器普通重启后保留。若要跨容器删除与重建保留日志，需使用保留的 volume 或 bind mount；tmpfs 不是持久存储。

使用专用的、规范的容器内绝对目录。cdh 以 `0700` 创建目录，以 `0600` 创建文件；已有的 cdh 日志文件和目录必须具备相应权限，并归运行时的有效 UID 所有。祖先必须是真实目录，由 root 或该 UID 拥有，且不得具有不安全的写权限。符号链接、意外硬链接、不安全对象以及第二个写入者会被拒绝。cdh 不会递归改变挂载目录的权限或所有者。

挂载父目录，让 cdh 创建自己的私有子目录。例如，使用镜像默认用户运行：

```bash
docker run --gpus all --name comfyui \
  --mount type=volume,source=cdh-logs,target=/logs \
  --env CDH_LOG_DIRECTORY=/logs/cdh \
  IMAGE
```

已有的 `0755` volume 根目录可以作为这里的父目录，但它本身不满足日志目录的 `0700` 要求。直接将这种根目录选为日志目录，会使文件记录回退到内存。重用日志目录前先停止旧容器；并发运行的多个副本需要不同目录。可以使用普通工具读取文件，但在 cdh 管理期间应保持其文件名、锁标记与轮转内容不变。

### 故障、缺口与命令结果

文件记录失败时，cdh 会告警，此后新增日志只保留在内存中，直到容器重启。实时输出继续，安全的已有文件仍可读取。修复存储问题后重启容器，才能重新尝试文件记录；cdh 不会自动恢复，也不会把遗漏的内存输出补写回文件。

仅出现存储警告，不会使内容完整的查询失败。已知缺口、读取失败或查询期间丢失所请求的输出，都会在 stderr 报告，并以非零状态退出；查询全部历史仍会返回缺口两侧的可用输出。近期 `--tail` 查询所需的输出完整时，仍可成功。已被正常保留策略移除的旧输出不是错误。初始历史不完整时，`--follow` 会以非零状态退出，不进入实时输出。

跟不上的客户端可能被断开，但不会因此停止 ComfyUI 或拖慢原始输出。`Ctrl-C` 只结束客户端，并返回 130。跟随可跨 ComfyUI 重启持续；容器重启后需要手动重新连接。意外断开或无法完成投递会返回非零状态。成功不保证后续关闭阶段的输出已送达，也不保证日志已保存到磁盘。

正常关闭时，cdh 会在可用的关闭时间内尝试保存待写入的日志。强制终止、关闭时间耗尽或宿主机/存储故障可能丢失末尾输出，因此保留历史不是完整审计记录。早期启动错误可能发生在日志记录可用之前；请使用 Docker logs 或部署后端查找这些诊断。详细的完成与关闭保证见[日志存储与查询契约](../dev/contracts.md#retained-logs-are-bounded-optional-copies)。

## 文件、下载与持久状态

主机端的 HTTP `[[files]]` 声明会成为固化到镜像中的运行时默认配置；宿主机本地构建文件不会。运行时只接受 `type = "http"` 条目；挂载配置中的本地来源会被拒绝，而不会尝试在容器内解释宿主机路径。每个 HTTP 条目使用 `source` 表示带 host 的 URL，使用 `target` 表示相对于 `COMFYUI_PATH` 的确切最终文件路径；运行时不接受目录 target、根 target，也不会从 URL 推断 target 文件名。比较 identity 前，多余的 `/` 和 `.` 路径段会被规范化；空值、绝对路径、明确的 `..`、反斜杠、控制字符和末尾斜杠均无效。靠后层中已有目标的条目会在原位置修补该条目，并保留它省略的字段；新目标会追加。靠后的 `files = []` 会清空之前的列表。合并后，重复或重叠的生效 target 会失败，挂载的 runtime 文件列表不能包含 local 条目。

运行时目标沿用构建目标的[文件命名规则](configuration.zh-CN.md#在镜像构建期间添加文件)：完整目标路径的任何一段名称都不能是 `.cdh-staging`，也不能以 `.wh.` 开头。

同步下载会在 pre-start Hook 之前完成。异步下载会在 ComfyUI 启动前被接收到一个后台队列中，并且可以在 ComfyUI 运行期间继续；它们不会阻塞 ComfyUI readiness。

`download_max_attempts` 是每个文件在一次容器启动或已接纳的 restart 中允许调用下载后端的总次数，其中包括第一次尝试。`download_failure_policy` 只在运行时适用：

- 对于同步文件，`fail` 会在出现常规最终失败或尝试预算耗尽后中止启动，而 `continue` 会继续处理后续文件；
- 对于异步文件，`fail` 会停止队列中的剩余任务，但不会停止 ComfyUI，而 `continue` 会继续处理后续排队文件；以及
- 路径边界、不安全目标类型、权限、身份、持久化和耐久性方面的失败一律采取失败关闭策略，不能被 `continue` 转为继续执行。

构建时文件遵循不同的约定：每个声明的构建文件都是必需的，并会权威替换 lower image 中的内容。下文的 `overwrite` 设置仅用于运行时。请参阅[构建与锁定指南](build-and-lock.zh-CN.md#构建文件与本地上下文-materialization)。

可选的 `checksum = "sha256:<64 hexadecimal digits>"` 声明可信内容的身份。应从相对于你的威胁模型足够独立的来源获取摘要；cdh 不会从下载来源获取或推断它。

| 现有目标 | `overwrite` | 结果 |
| --- | --- | --- |
| 与配置的 checksum 匹配 | 任一值 | 保留已验证的文件 |
| 与配置的 checksum 不匹配 | `false` | 保留现有文件并失败 |
| 与配置的 checksum 不匹配 | `true` | 仅在完整的新文件通过验证后替换 |
| 未配置 checksum | `false` | 保留现有的常规文件，并视为未经验证 |
| 未配置 checksum | `true` | 传输完成后进行原子替换，但不声称内容具有真实性 |

在完整的替换文件准备好进行原子发布前，cdh 会保持现有最终文件不变。没有 checksum 时，传输成功和原子替换并不能证明下载的字节是真实可信的。

如果操作在替换目标后失败，完整的新文件可能已经位于目标位置；cdh 不会恢复旧文件。重试前请检查目标。

运行时协调状态位于 `/var/lib/cdh/runtime/state.json`。此文件是 cdh 拥有的内部恢复状态，不是用户配置或下载历史 API。请勿编辑。运行时下载要求状态位置可写。挂载 `/var/lib/cdh/runtime` 可在容器替换后保留恢复状态，同时还需挂载每个必须保留下载文件的目标目录。仅保留状态文件并不会保留已下载的文件。

使用相同的持久状态和下载目标启动替代容器前，必须先停止旧的 cdh 容器。不要让重叠运行的实例或多个副本写入同一个状态文件或相同的下载目标；使用不同的状态文件也不能让共享目标变得安全。

## 经过认证的 HTTPX 下载

构建时 downloader route 与 Secret definition 永远不会固化到运行时配置。要认证运行时下载，请在挂载的 `/etc/cdh/runtime/config.toml` 中独立声明 route 与容器可见 Secret source：

```toml
[secrets.hf_read]
file = "/run/secrets/hf_read"

[[cdh.downloader.credentials]]
match = "https://huggingface.co/acme/private-model/"
type = "bearer"
token = { secret = "hf_read" }

[[files]]
type = "http"
source = "https://huggingface.co/acme/private-model/resolve/main/model.safetensors"
target = "models/checkpoints/model.safetensors"
downloader = "httpx"
```

运行时 Secret 必须且只能选择一个 `env` 或 `file` source。环境变量 locator 指向容器启动环境中已经存在的变量；文件 locator 必须是容器内绝对路径。cdh 会跟随 Kubernetes Secret mount 等由部署系统管理的 symlink projection，然后要求解析后的对象是普通文件，并从同一个已打开 descriptor 中最多读取 65,525 bytes。它不会对 `0444`、`0644` 等 projected-file mode 给出警告；mount mode、ACL、namespace 与相同 UID 的访问边界均由部署负责。

每个运行时 generation 会先校验 route 结构和 reference，而不会读取 Secret 内容。文件 reconcile 完成后，generation 只在首次实际发送受保护 outbound request 之前读取所选 Secret，并在该 generation 剩余生命周期内把值缓存于内存。已经完成、无需安排网络请求的 target 不要求 Secret 可用。一次被接受的 runtime restart 会丢弃上一 generation 的 snapshot，并重新解析每个实际需要的 source，因此替换 projected file 后 restart 可以轮换值；改变进程环境通常需要重新创建容器。

Secret 缺失、不可读或 Bearer 内容无效时，会在本地失败且不会重试该 credential failure。初始受保护 request 发送前失败时消耗零次 network attempt；如果公开 request redirect 进入受保护 route，已经完成的 attempt 仍会保留。之后由生效的 `download_failure_policy` 按既有同步或异步队列语义处理。401、403 等真实 HTTP response 仍属于普通下载失败。

Route definition、Secret reference/locator、解析值与 value hash 都不会进入 runtime desired-content identity 或持久化 download state。因此 credential 轮换不会重新下载已完成文件；新 generation 中尚待处理的工作会使用该 generation 的值。cdh 不会把 token 或生成的 Authorization 值写入 state、status、history、manifest 或自有日志。以相同容器 UID 运行的代码仍属于部署信任边界；cdh 不会把它与部署设置为可读的 Secret 隔离。

## SSH 与机密值

SSH 提供选择性启用的 root 访问，默认处于禁用状态。可在运行时 TOML 中启用，或设置 `SSH_ENABLE=true`，并提供至少一个有效的公钥或密码。凭据本身不会启用 SSH。如果启用了 SSH 但没有有效凭据，cdh 会发出警告、不启动 sshd，并继续正常启动 ComfyUI。

应优先在容器启动时提供 `SSH_PUB_KEY` 或 `SSH_PASSWORD`，而不是将凭据固化到镜像中。`SSH_PUB_KEY` 会向配置的密钥集合追加一个规范化后的公钥。启用 SSH 时，容器会在启动期间生成自己的主机密钥；由 cdh 构建的镜像不会共享软件包生成的主机密钥。

运行时公钥采用[配置指南](configuration.zh-CN.md#对配置进行分层)所述的相同普通行语法和受支持安全密钥算法。不接受 `authorized_keys` options 前缀。

经过认证的 SSH 会话会继承容器启动时的生效环境，包括镜像 `ENV`、`[system.env]`，以及 Docker 或其他 OCI 运行时新增或覆盖的值。OpenSSH 会为当前连接提供 `TERM` 和 `SSH_AUTH_SOCK`；cdh 不会用容器启动时的同名值替换它们。其他变量不会根据名称或内容被过滤。

因此，经过认证的 root 会话可以检查继承的密码、token 和 Secret source 环境值。镜像构建者和部署操作者须对写入该环境的所有值负责；cdh 提供的 SSH 访问不是 Secret 隔离边界，也不会对 sshd、shell profile 或认证后所执行命令产生的输出进行脱敏。

如果已经使用凭据启用 SSH，但 cdh 无法保留完整环境或准备并启动 sshd，则容器启动会失败，而不会提供功能不完整的 SSH 服务。如果 sshd 在 ComfyUI 启动后意外退出，cdh 会发出警告，但不会停止 ComfyUI。配置的 SSH 端口位于容器内部；主机端口发布和网络暴露由 Docker 或部署平台负责。

与 SSH 关联且会加载镜像系统 profile 的 login shell 会自动进入生效的 `WORKSPACE`。这适用于默认的交互式 `ssh root@host` 登录，也适用于显式请求且会加载系统 profile 的 login-shell 命令，例如 `ssh root@host 'bash -lc "pwd"'` 或 `ssh root@host 'sh -lc "pwd"'`。不加载系统 profile 的 shell 启动方式不会获得这项便利。`ssh root@host pwd` 之类的普通远程命令不会调用 login shell，而是从 `/root` 开始。如果加载该 profile 的 login shell 无法进入 `WORKSPACE`，它会切换到 `/root`，输出固定警告 `Warning: cdh could not enter WORKSPACE; continuing in /root`，然后继续运行。

当前 cdh 构建的镜像会把 cdh、uv 和配置的 uv 工具放在镜像的默认工具路径中。在此行为可用之前构建的镜像需要重新构建；仅更改运行时配置无法更新现有镜像内容。

cdh 创建 `/root/.ssh` 和 `authorized_keys` 时会分别使用 `0700` 和 `0600`。现有 `.ssh` 目录在由 root 拥有且同组用户和其他用户均不可写时可被接受；安全但不是 `0700` 的权限会原样保留并产生警告。该目录仍须实际允许 cdh 创建临时文件并执行原子替换；只读挂载、访问控制或 Linux capability 限制以及其他 I/O 失败仍会导致启动失败。现有由 root 拥有的普通 `authorized_keys` 文件在同组用户和其他用户均不可写时可被替换；安全但不是 `0600` 的权限会产生警告，而原子替换后的新文件仍为 `0600`。所有者不正确、同组用户或其他用户可写、符号链接和特殊文件也仍会导致启动失败。

原子替换会改变 `authorized_keys` inode。直接绑定挂载（bind mount）该文件的部署可能拒绝替换；应以安全权限挂载父 `.ssh` 目录，或改为通过运行时配置提供密钥。cdh 不会回退为原地写入凭据。

root SSH 会扩大容器的攻击面。应相应地保护配置、环境变量值、渲染上下文、镜像产物、注册表、日志和运行时访问。cdh 会避免打印明确提供的 SSH 密码，并将自己的临时凭据保持在内部，但它不会猜测任意 TOML 值、URL、参数或环境变量是否属于秘密。

## Runtime Hook 与启动 readiness

向 `cdh host render` 或 `cdh host build` 传入 `--runtime-hooks-dir <dir>` 以固化 runtime Hook 目录树。不传此选项时，不会固化任何 runtime Hook。请参阅 [runtime Hook 示例](../../examples/runtime-hooks/)。

该目录树使用以下阶段目录：

```text
pre-start.d/
post-start.d/
stop.d/
```

只有直接位于阶段目录中的普通 `.sh` 或 `.py` 文件会被选为 Hook。其他后缀的普通文件和普通目录会在不递归遍历的情况下被忽略，并按来源和阶段产生聚合后的简明警告。symlink、特殊文件、检查/读取失败和无效的已知阶段路径仍会导致启动失败。Shell Hook 使用 `bash` 运行；Python Hook 使用托管的应用程序 Python。Hook 会接收容器运行时环境，并以 `COMFYUI_PATH` 作为工作目录。

固化的 Hook 是位于 `/opt/cdh/runtime/hooks` 下经过选择和内容验证的镜像输入。你还可以在 `/etc/cdh/runtime/hooks` 挂载部署 Hook；挂载的 Hook 仍是外部运行时输入，不属于镜像 lock。固化的 Hook 先于挂载的 Hook 运行；在每个来源和阶段内，文件名按字典序运行。

固化和挂载的 Hook 都是受信任的可执行代码。cdh 会验证选定的固化字节，但不会将 Hook 置于沙箱中，也不会让 Hook 对文件系统、网络、软件包或进程产生的影响变得可复现。

启动顺序如下：

```text
synchronous downloads
  -> pre-start hooks
  -> optional sshd
  -> asynchronous queue acceptance
  -> ComfyUI
  -> conditional readiness
  -> post-start hooks
```

只有在至少存在一个 post-start Hook 时，cdh 才会等待 readiness。它通过回环地址探测有效 ComfyUI 端口上的 `/system_stats`，并要求响应是包含 `system` 和 `devices` 的 HTTP 200 JSON 对象。如果 ComfyUI 在达到 readiness 前退出，或者有界的 readiness 等待超时，则启动失败，且 post-start Hook 不会运行。

这套完整的启动顺序会用于容器初始启动和每次已接纳的 restart。

此 readiness 检查表示 ComfyUI API 在启动初始化后正在提供服务。它不是通用的容器健康检查，也不能证明每个自定义节点、工作流、模型、GPU 路径或生产工作负载都能正常工作。

## 由 Hook 启动的后台服务

当 Hook 的主进程仍在运行时，cdh 负责该 Hook。主进程结束后，cdh 不会发现、监督、进行健康检查或向该 Hook 有意留在后台运行的进程发送信号。

如果启动 Hook 启动了一项服务，应为它配套一个 stop Hook，通过该服务自身的控制接口，或经过仔细验证的进程身份，请求终止并等待退出。缺失或失败的 stop Hook、ComfyUI 自然退出、外部 `SIGKILL` 或容器过早终止，都无法为该服务提供优雅关闭保证。容器终止可以结束仍在运行的进程，但这不属于优雅的服务关闭。

## 信号与关闭

每个由 cdh 构建的镜像都以 Tini 作为 PID 1，并以 cdh 作为其直接子进程运行。Tini 会将容器的停止信号转发给 cdh，并回收被接管的孤儿进程。它不是服务监督器或健康检查器。

收到第一个 `SIGTERM` 或 `SIGINT` 时，cdh 会：

1. 停止接收异步工作，并开始取消下载队列，同时停止 sshd；
2. 在 ComfyUI 仍可用时按顺序运行 stop Hook；
3. 将原始信号转发给 ComfyUI；以及
4. 等待由 cdh 管理的进程退出并被回收。

`shutdown_timeout` 是停止当前 ComfyUI 运行时的一份基于单调时钟计算的总时间预算，无论关闭由外部信号还是已接纳的手动 restart 发起。默认值为八秒，其中最后两秒预留给向 ComfyUI 发送信号和回收受管理的子进程。前段的 Hook 预算耗尽时，cdh 会终止当前活动的 Hook 并跳过后续 Hook。到达总截止时间时，它会强制停止仍在运行的受管理工作。在 restart 期间接纳的 Docker shutdown 会取得优先权、阻止 ComfyUI 再次启动，且不能延长已经开始计时的截止时间。

第二个 `SIGTERM` 或 `SIGINT` 会跳过剩余的宽限期并立即进入强制关闭。被强制终止的 ComfyUI 通常会使容器以代码 137 退出。当 ComfyUI 自然退出时，cdh 会保留其退出结果、清理辅助工作，并且不会运行仅限信号路径的 stop Hook。

Docker 或其他编排器拥有独立的外部硬性时间限制。未配置容器专用超时时，Docker Engine 对 Linux 容器使用 10 秒的默认值，而 Docker Compose 的 `stop_grace_period` 默认为 10 秒。cdh 的八秒默认值只留下尽力而为的调度余量。当 Hook 需要更多时间时，请将 Docker [`--stop-timeout`](https://docs.docker.com/reference/cli/docker/container/run/#options) 或 Compose [`stop_grace_period`](https://docs.docker.com/reference/compose-file/services/#stop_grace_period) 配置为大于 cdh 总时间。

设置 `shutdown_timeout = -1` 只会禁用外部关闭和手动 restart 所使用的 cdh 外层与 Hook 截止时间。由 cdh 管理的组件操作仍然有界，而 Docker 自身的超时与之独立。外部 `SIGKILL` 后无法继续执行任何清理。
