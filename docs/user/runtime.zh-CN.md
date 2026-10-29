# 运行和管理 ComfyUI

[English](runtime.md) | 简体中文

本指南面向运行 cdh 镜像的操作者。从[构建指南](build-and-lock.zh-CN.md)生成的本地镜像（例如 `my-comfy:dev`）开始，再按需添加部署设置。

## GPU 宿主机要求

GPU 运行需要 NVIDIA Container Toolkit 支持、版本不低于 `580.65.06` 的 NVIDIA 驱动，以及 Turing 或更新的 NVIDIA GPU。请在具备这些条件的 Docker 主机上运行 Linux `amd64` 镜像。

## 启动和停止 ComfyUI

在具备 GPU 支持、已加载 `my-comfy:dev` 的 Docker 主机上，启动命名容器，将默认端口发布给本机客户端：

```bash
docker run -d --name comfyui --gpus all \
  -p 127.0.0.1:8188:8188 \
  my-comfy:dev
docker logs --follow comfyui
```

启动后，在该主机打开 [http://localhost:8188](http://localhost:8188)。使用自己的模型运行一个工作流，检查 GPU 是否正常工作。页面打不开时，先看 Docker 日志中的启动错误，再检查端口映射。以上回环地址映射不会向其他机器开放界面。

`Ctrl-C` 只关闭日志查看器，不停止后台容器。停止和再次启动同一个容器：

```bash
docker stop comfyui
docker start comfyui
```

停止后执行 `docker rm comfyui` 会删除容器及其可写层。替换前应通过挂载保留所需数据，见[持久化](#文件下载与持久化)。再次执行 `docker run` 前，该名称必须空闲。

## 修改运行设置

镜像自带默认配置。挂载运行配置目录即可调整受支持的设置，无需重建镜像。例如，在当前目录下创建 `runtime/config.toml`：

```toml
[comfyui]
port = 8188
extra_args = []

[cdh]
shutdown_timeout = 20
```

在 POSIX shell 中，挂载该目录启动新容器：

```bash
docker run -d --name comfyui --gpus all \
  -p 127.0.0.1:8188:8188 \
  --mount "type=bind,source=$(pwd)/runtime,target=/etc/cdh/runtime,readonly" \
  --stop-timeout 30 \
  --env TZ=Asia/Shanghai \
  my-comfy:dev
```

其他宿主机请使用所用 shell 的绝对路径写法。挂载目录后，替换 `config.toml` 的新文件可在应用重启时被读取。若已有同名容器，先保存数据，再停止并删除它。

优先级为：**内置默认值 → 镜像默认值 → 挂载配置 → 容器启动环境变量**。后者覆盖前者。运行 TOML 中已知的构建专用字段会被忽略并警告；未知字段或无效的最终配置会使启动失败。

| 修改内容 | 生效方式 |
| --- | --- |
| 挂载的 ComfyUI、下载、SSH 设置或钩子 | 在容器内执行 `cdh container runtime restart` |
| 文件形式的下载令牌 | 替换文件并重启应用；待处理请求会读取新值 |
| 有效的日志记录设置 | 重启整个容器；应用重启仍沿用原记录设置 |
| 容器环境变量、端口发布或挂载 | 用新的 Docker 选项重建容器 |
| 已安装软件包、ComfyUI 版本、镜像内置文件或钩子 | 重建镜像，再重建容器 |

挂载配置的表会继承省略的字段。普通列表整体替换，`[]` 清空；文件按目标合并，凭据规则按匹配范围整体替换，命名凭据来源也整体替换。`SSH_PUB_KEY` 是例外：配置合并后追加一个公钥，已有相同密钥时忽略。

监听地址和端口使用 `comfyui.listen`、`comfyui.port`，或 `CDH_COMFYUI_LISTEN`、`CDH_COMFYUI_PORT`。不要把 `--listen`、`--port`、`--auto-launch`、`--disable-auto-launch` 放入 `extra_args`。`CDH_COMFYUI_EXTRA_ARGS` 按 shell 风格拆分单词，但不执行 shell。`TZ` 为遵循它的程序覆盖镜像时区。其他容器启动覆盖项如下：

| 设置 | 环境变量 |
| --- | --- |
| 默认下载器和模式 | `CDH_DEFAULT_DOWNLOADER`、`CDH_DEFAULT_DOWNLOAD_MODE` |
| 尝试次数和失败策略 | `CDH_DOWNLOAD_MAX_ATTEMPTS`、`CDH_DOWNLOAD_FAILURE_POLICY` |
| 关闭预算 | `CDH_SHUTDOWN_TIMEOUT` |
| SSH 访问 | `SSH_ENABLE`、`SSH_PORT`、`SSH_PASSWORD`、`SSH_PUB_KEY` |

设置值和 `CDH_LOG_*` 日志覆盖项见[完整示例](../../examples/full.toml)。

## 重启和查看应用状态

```bash
docker exec comfyui cdh container runtime restart
docker exec comfyui cdh container runtime status
docker exec comfyui cdh container runtime status --json
```

重启会停止当前应用及辅助工作，重新读取配置和钩子，再执行启动顺序。它仍使用原来的容器环境；仅传给 `docker exec` 的变量不会变成运行设置。控制命令应使用容器默认用户，其他 UID 无法访问控制器。若部署覆盖 `PATH`，必须保留 `/opt/uv/bin`。

| 情况 | 结果 |
| --- | --- |
| 正常重启 | 等到 ComfyUI 进程启动；有启动后钩子时，还会等待就绪及钩子完成 |
| 异步下载 | 接受入队即可，不等待所有传输完成 |
| 已有重启正在进行 | 新请求报告忙碌并失败 |
| 请求接受前按 `Ctrl-C` | 取消请求 |
| 请求接受后按 `Ctrl-C` | 结束本地等待，操作继续；用 `status` 查看 |
| 重启失败 | 报告失败、清理并让容器以非零状态退出 |

cdh 没有运行时 `start` 或 `stop` 命令，请用 Docker 管理容器。使用运行控制命令前，应先启动容器。ComfyUI 自然退出也会结束容器。状态查询显示当前应用和重启状态；模型及 GPU 是否正常，应通过工作流检查。

## 查看和保留日志

启动错误优先查看 Docker 日志。cdh 还会限量记录应用、钩子、服务和生命周期输出，供容器内查询：

```bash
docker exec comfyui cdh container runtime logs --tail 200
docker exec comfyui cdh container runtime logs --tail 200 --follow
docker exec comfyui cdh container runtime logs --tail 0 --follow
docker exec comfyui cdh container runtime logs > runtime.log
```

不指定条数时返回全部保留历史，`--tail all` 效果相同；`-n` 和 `-f` 是简写。`--tail 0 --follow` 只看新输出。持续查看可以跨应用重启，但容器重启后需要重新连接。`Ctrl-C` 只关闭客户端，退出码为 130。

查询将捕获的 stdout/stderr 合并到 stdout，诊断仍在 stderr。不收集任意日志文件或独立 exec/SSH 会话。ComfyUI 和子进程输出不会被 cdh 脱敏或过滤。cdh 详细度选项 `-q`、`-v`、`-vv` 放在 `container` 之前；给日志客户端设置它们不会改变正在运行的控制器详细度。

在镜像或挂载 TOML 中配置 `[cdh.logs]`：

| 模式 | 可用历史 |
| --- | --- |
| `file`（默认） | 轮转文件和近期内存输出，不重复返回相同字节 |
| `memory` | 近期保留在内存中的输出，不读取旧文件 |
| `none` | 不记录新历史；仍可读取已有的 cdh 日志文件，同时警告 |

所有模式都可只看实时输出，记录选择不会关闭 Docker 输出。`none` 模式从历史进入实时查看时，会警告未记录的间隔。没有历史文件时，查询成功返回空结果。

精确设置和大小写法见[完整示例](../../examples/full.toml)。默认最多保留 20 MiB 内存内容和 100 MiB 文件内容。`max_size` 分别限制内存和每个文件；`max_files` 包括当前文件。进程还有其他内存开销。

### 持久化日志

内存可跨应用重启保留，不能跨容器重启。已写入的文件可在同一容器重启后保留；替换容器时，需要卷或绑定挂载。tmpfs 不提供持久化。

挂载父目录，让 cdh 创建私有子目录。创建新容器时添加：

```text
--mount type=volume,source=cdh-logs,target=/logs
--env CDH_LOG_DIRECTORY=/logs/cdh
```

专用日志目录必须属于运行用户，权限为 `0700`，文件为 `0600`。祖先必须是真实且所有权安全的目录，链接或不安全条目会被拒绝。常见的 `0755` 卷根目录适合作为父目录，不适合直接作为私有日志目录。cdh 不会递归修复挂载权限。替换容器复用日志目录前，先停止旧容器；并行副本需要独立目录。cdh 管理期间不要修改其日志文件或轮转标记。

### 处理记录和查询失败

| 现象 | 处理或限制 |
| --- | --- |
| 文件记录失败 | cdh 警告，后续输出仅记入内存；修复存储并重启容器后重试，不回填遗漏的文件内容 |
| 查询报告缺口或读取失败 | 可能仍返回可用内容，但命令失败；历史不完整时不进入持续查看 |
| 旧输出被正常轮转删除 | 不属于错误；按需增加保留量或使用外部日志系统 |
| 慢客户端被断开 | 重新连接；它不会拖慢主输出或停止 ComfyUI |
| 很早的启动错误 | 查 Docker 日志，此时 cdh 记录可能尚未建立 |

单独的存储警告不会使完整查询失败。即使较早历史有缺口，近期尾部查询仍可能成功。意外断连会使客户端失败。强制关闭、时限或存储故障可能丢失最后的输出。实现细节见[日志契约](../dev/contracts.md#retained-logs-are-bounded-optional-copies)。

## 文件下载与持久化

构建时的 HTTP 文件声明会成为运行默认值，本地来源不会。运行时只接受 HTTP 文件，采用[相同的目标命名规则](configuration.zh-CN.md#在构建时添加文件)。相同目标的后续条目补充或覆盖字段，新目标追加，`files = []` 清空列表；最终目标不能重叠。

| 下载模式 | 启动行为 |
| --- | --- |
| 同步 | 在启动前钩子之前完成 |
| 异步 | 在 ComfyUI 启动前进入后台队列，不阻塞就绪判断 |

`download_max_attempts` 包括首次尝试。普通失败耗尽次数后，`download_failure_policy = "fail"` 对同步下载会中止启动；对异步下载则停止剩余队列，不停止 ComfyUI。`continue` 继续处理后续文件。路径、权限或文件状态不安全，以及数据或恢复状态保存失败，都会终止操作。构建时下载始终必须成功。

需要校验下载内容时，请提供来自可信来源的 `sha256:` 校验值。

| 已有目标 | `overwrite` | 结果 |
| --- | --- | --- |
| 与校验值相符 | 任意 | 保留已验证文件 |
| 与校验值不符 | `false` | 保留已有文件并失败 |
| 与校验值不符 | `true` | 新内容验证成功后替换 |
| 未提供校验值 | `false` | 保留已有普通文件 |
| 未提供校验值 | `true` | 下载完成后替换 |

替代文件准备完成前，旧文件保持不变。替换后的后续操作若失败，新文件可能已存在，应先检查再重试。没有校验值时，cdh 无法将文件与预期的内容摘要比较。

跨容器替换保留下载，需要挂载下载目标目录，**同时**挂载 `/var/lib/cdh/runtime` 保存恢复状态。使用默认 ComfyUI 路径时，模型可用命名卷挂载到 `/workspace/ComfyUI/models`。只保留状态不保留文件。不要编辑 `state.json`，也不要让重叠运行的容器写同一状态或目标；先停止旧容器。

## 认证下载与凭据轮换

运行时不继承构建凭据。在挂载的运行 TOML 中独立声明来源、规则和文件：

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

让令牌在该容器绝对路径可读，或用 `env` 选择容器启动时已有的变量。通过 Docker 轮换文件时，将其所在目录只读绑定挂载到 `/run/secrets`，使替换后的文件仍可见。遵循[令牌格式和匹配规则](configuration.zh-CN.md#为-httpx-下载提供认证)。支持 Kubernetes Secret 等由部署系统管理的符号链接投影，但解析后的目标必须是普通文件。部署方负责权限及同用户其他代码的访问。

轮换文件令牌：

1. 替换挂载来源的值。
2. 执行 `docker exec comfyui cdh container runtime restart`。
3. 查看日志中需要新值的待处理下载。

值在首次受保护请求需要时读取，之后复用到应用重启。已完成且无需请求的文件不需要令牌，也不会仅因令牌变化而重新下载。环境变量来源需要重建容器才能改变。

缺失、不可读或格式错误的令牌会在本地失败，不重试该凭据错误。HTTP 401/403 属于普通下载失败，仍遵循同步或异步失败策略。cdh 不将令牌写入状态或自己的日志。同一用户运行的其他代码仍可读取该用户有权访问的凭据。

## SSH 访问

SSH 默认关闭，启用后提供 root 访问。优先在容器启动时提供凭据，不要固化到镜像。设置 `SSH_ENABLE=true`，并提供至少一个有效的 `SSH_PUB_KEY` 或 `SSH_PASSWORD`；仅有凭据不会启用 SSH。容器 SSH 端口的发布由 Docker 管理。

从 OpenSSH `.pub` 文件复制普通公钥行。支持安全密钥 Ed25519 和 ECDSA P-256，不接受证书或 `restrict`、`from=`、`command=` 等 `authorized_keys` 前缀。TOML 公钥列表替换前一列表，`SSH_PUB_KEY` 则追加一个公钥，已有相同密钥时忽略。

| 情况 | 结果 |
| --- | --- |
| 启用但没有有效凭据 | 警告，继续运行但不提供 SSH |
| 启用且有凭据，但准备失败 | 容器启动失败 |
| ComfyUI 启动后 sshd 退出 | 警告，ComfyUI 继续运行 |

容器自行生成主机密钥。认证后的 root 会话继承容器启动环境，包括放在其中的凭据；每个连接的 `TERM` 和 `SSH_AUTH_SOCK` 仍由 OpenSSH 提供。因此，只向你信任的人授予 SSH 访问权限。

读取系统 profile 的登录 shell 会进入 `WORKSPACE`，失败时警告并退回 `/root`。`ssh root@host pwd` 这样的普通远程命令不读取 profile，从 `/root` 开始。

优先通过运行配置提供公钥。若挂载 `.ssh`，应挂载所有权安全的父目录，而不是单独挂载 `authorized_keys`：cdh 原子替换文件，直接文件挂载可能阻止替换。已有条目必须属于 root，且组或其他用户不可写；不安全链接、所有权、只读挂载或写入失败会阻止启动。cdh 以 `0700` 创建 `.ssh`、以 `0600` 写入公钥；安全但较宽松的已有权限会警告。

## 运行时钩子与启动顺序

用 `--runtime-hooks-dir` 将脚本加入镜像，或将部署脚本挂载到 `/etc/cdh/runtime/hooks`。每个来源包含 `pre-start.d`、`post-start.d` 和 `stop.d`。只执行直接的普通 `.sh`、`.py` 文件；其他普通条目忽略并警告，不安全条目或读取失败会阻止启动。

同一阶段先执行镜像内钩子，再执行挂载钩子，各来源内按文件名字典顺序执行。Shell 脚本使用 Bash，Python 脚本使用应用解释器，工作目录为 `COMFYUI_PATH`。两者都是可信代码；挂载钩子不受镜像锁定。见[示例](../../examples/runtime-hooks/)。

首次启动和接受的重启均按以下顺序执行：

1. 完成同步下载。
2. 执行启动前钩子，启动可选 SSH。
3. 接受异步下载队列，启动 ComfyUI。
4. 如果有启动后钩子，等待就绪，再执行它们。

有启动后钩子时，cdh 会等 ComfyUI 的 API 可响应后再执行它们；进程退出或等待超时会使启动失败。没有这些钩子时，cdh 启动 ComfyUI 后不等待 API 就绪。

钩子启动后台服务后自身退出，cdh 就不再监督该服务。应配套停止钩子，通过服务控制接口或经过验证的进程身份请求关闭并等待退出。这样可让服务在容器退出前有机会完成收尾工作。

## 信号与关闭

Docker 停止信号经 Tini 传给 cdh。收到第一次 `SIGTERM` 或 `SIGINT` 后，cdh 取消下载和 SSH，在 ComfyUI 仍可用时执行停止钩子，再向 ComfyUI 发信号并等待所管理的进程退出。

| 设置或事件 | 效果 |
| --- | --- |
| `shutdown_timeout`（默认 8 秒） | 关闭或重启的总预算，最后两秒留给 ComfyUI 信号和子进程回收 |
| 钩子时间用尽 | 终止当前钩子，跳过后续钩子 |
| 总时间用尽，或收到第二次信号 | 强制关闭；ComfyUI 被强杀时通常返回 137 |
| 应用重启期间收到 Docker 停止请求 | 关闭优先，不再启动 ComfyUI |
| ComfyUI 自然退出 | 保留退出结果，清理辅助工作，不执行仅由信号触发的停止钩子 |
| `shutdown_timeout = -1` | 关闭 cdh 外层和钩子期限，不关闭组件限制或 Docker 期限 |

Docker `--stop-timeout` 或 Compose `stop_grace_period` 应大于 cdh 总预算；前面的示例为 cdh 设置 20 秒、Docker 设置 30 秒。外部硬时限独立生效。收到 `SIGKILL` 后无法继续清理。
