# 配置

规范性来源：[English](configuration.md) | 简体中文

本指南面向为 cdh 选择和组合 TOML 输入的用户。[严格配置模型](../../src/comfyui_docker_helper/config/authored/models.py) 和验证代码是机器权威；本指南说明面向用户的选择，而不重复每一个字段。

## 选择起始示例

- [`minimal.toml`](../../examples/minimal.toml) 是可运行且受支持的最小配置，也是通常使用的起点。
- [`full.toml`](../../examples/full.toml) 是附有全面注释的配置参考。其生效 TOML 可保持离线验证通过，但它不是端到端构建配置。

示例中的每一个值都是该示例作出的显式选择，并不代表隐含的默认值。需要可运行配置时请从最小示例开始，并按需查阅或复制完整参考中的配置段。

在本地验证配置，无需访问网络、使用 Docker 或执行写入：

```bash
cdh host validate -f examples/minimal.toml
cdh host validate \
  -f examples/full.toml \
  --build-hooks-dir examples/build-hooks
```

使用 `cdh host validate --help` 查看当前命令选项。

## 对配置进行分层

重复使用 `-f/--file` 可按命令行中的顺序合并 TOML 文件。表会递归合并；靠后的标量或普通数组会替换靠前的值。以下组合型集合改为按各字段专属的标识合并：

- `system.extra_packages` 使用允许的 Debian 包名；
- `python.extra_packages`、`python.uv_tools` 和 `pytorch.extra_packages` 使用完整的 canonical requirement，其中包括规范化的分发包名、规范化并排序后的 extras、selector 或具名 direct reference，以及 marker；
- `comfyui.custom_nodes` 使用仅转为小写的 Registry 资源 ID，或精确的直接 Git URL；
- `files` 使用规范化后的直接 `target` 标识；
- `cdh.downloader.credentials` 使用 `match` 表示的 canonical HTTP(S) origin 与路径；
- `cdh.git.credentials` 使用 `match` 所表示的 canonical credential context。

对于包集合，新标识会按首次出现顺序追加。在具有唯一键的不同层之间完全重复的 Debian 包只保留一次。如果生效的 `system.extra_packages` 条目已属于 cdh 默认 OS 包集合，cdh 会在其来源位置给出警告，并从生效安装请求中忽略这个冗余条目。在同一个用户编写的列表中声明的重复项仍是错误。Python requirement 只有在完整 canonical requirement 相等时才会跨层去重；cdh 不推断一般意义上的版本范围等价关系。同一规范化分发包如果 extras、selector、direct source 或 marker 不同，会保留到针对所选目标完成 marker 求值为止。之后，如果应用软件包或隔离工具中存在多条该分发包的生效声明，它们会冲突。同一层中编写的重复项也会保留给校验处理。靠后的空列表会重置对应集合。

`system.ssh.pub_keys` 在 TOML 层之间仍采用普通的整列表替换：省略会继承，靠后的非空列表会替换，`[]` 会清空。选出最终生效列表后，cdh 会裁剪每行首尾空白、丢弃空值，并按声明的密钥类型加 base64 密钥 blob 进行稳定去重。它会保留第一条规范化后的完整行及其可选注释。之后每个非空重复项都会产生带来源的警告，且警告不会打印密钥内容。

请从标准 OpenSSH 工具生成的 `.pub` 输出中复制普通公钥行作为公钥值，不要包含 `authorized_keys` options 前缀。支持由认证器托管的 Ed25519 和 ECDSA P-256 密钥（`sk-ssh-ed25519@openssh.com` 和 `sk-ecdsa-sha2-nistp256@openssh.com`）。OpenSSH 证书以及 `restrict`、`from=`、`command=` 等 options 不属于可接受的配置语法。

Registry ID 的大小写变体表示同一资源，并在原位置覆盖，靠后编写的拼写最终生效。标点符号变体仍是不同的 Registry 资源；如果这些资源映射到同一个规范化的已安装 Python 分发包标识，生效配置校验会报告冲突，而不会擅自选择其中一个。

即使原始 `match` 字符串不同，canonical 等价的 credential context 也表示同一路由。靠后的路由会在原位置原子替换完整的靠前路由；路由字段不会逐字段合并。同一层中存在歧义的重复 route 仍然无效。靠后的 `credentials = []`、`custom_nodes = []` 或 `files = []` 会重置相应集合。每个 `[secrets.<name>]` 表也是原子来源定义，因此靠后的层可以用 `file` 替换 `env`，而不会保留旧字段。所有层生成生效配置后，才会检查严格结构、唯一性和跨字段规则。

对于 `[[files]]`，`target` 是相对于 `COMFYUI_PATH` 的一个直接相对 POSIX 路径。多余的 `/` 和 `.` 路径段会规范化为一个标识；空值、绝对路径、控制字符、明确的 `..`、反斜杠以及任何末尾斜杠均无效。HTTP 和本地文件的 target 必须是 `COMFYUI_PATH` 的严格后代，并表示一个确切文件。本地目录 target 相对于 `COMFYUI_PATH`，可以用 `target = "."` 等于其根目录；cdh 会在 render/build 准入期间判断本地来源是文件还是目录。相同规范化目标的 overlay 会修补相同 `type` 的条目；改变 `type` 会完整替换该条目，避免继承另一种来源专属的字段。生效的 target 区域不能通过相等或祖先/后代关系重叠。

例如，将以下内容保存为 `local.toml`，以禁用 comfy-cli，并移除完整示例所选择的节点和文件：

```toml
files = []

[comfyui]
install_cli = false
custom_nodes = []
```

然后验证生效配置：

```bash
cdh host validate -f examples/full.toml -f local.toml
```

## 受支持的选择

本软件包支持 Python 3.12 至 3.14。`python.version` 必须选择该范围内一个精确的稳定版 CPython 补丁版本；省略时默认为 `3.13.14`。cdh 不会静默切换到另一个 Python 版本。软件包支持范围由 [`pyproject.toml`](../../pyproject.toml) 定义。

当前计算后端是 CUDA，当前镜像目标是 `linux/amd64`。CUDA 和 PyTorch 版本必须显式选择。ComfyUI 同样需要显式选择器：`latest`、`nightly`、完整的小写提交哈希、带或不带 `v` 前缀的精确语义化发布版本，或受支持的版本约束。cdh 只从 ComfyUI 官方仓库解析该选择器，并且每个可接受的检出版本都必须派生自受支持的 v0.11.0 版本下限。

完整示例记录了可接受的选择器形式和其余默认值。不受支持的上游镜像标签、版本或目标组合会失败，而不会被静默替换。

## 设置持久的镜像环境变量

当需要在所选基础镜像中添加或覆盖非 Secret 的 Docker `ENV` 值时，请使用 `[system.env]`。不要重复声明基础镜像已经提供且受支持的工具链值。配置的值会保留在生成的镜像中，并可能出现在镜像 metadata、history 或日志里，因此不要在此放置 Secret；cdh 保留的运行时、路径和软件包管理器名称仍不允许覆盖。

例如，可以用 IANA 时区名称将非 Secret 的时区默认值固化到镜像中：

```toml
[system.env]
TZ = "Etc/UTC"
```

省略 `TZ` 时，cdh 不会添加时区值，并保留所选基础镜像的行为。

当 cdh 安装应用或自定义节点软件包时，生效构建容器环境中一部分受控的构建工具链变量可供软件包构建步骤使用。已有的受控代理处理和命令自有值仍保持独立；任意其他条目不会成为 installer 或 Python 配置。完整操作边界请参阅[软件包构建环境](build-and-lock.zh-CN.md#软件包构建环境)。

## 设置运行时日志记录默认值

使用 `[cdh.logs]` 将日志记录默认值固化到镜像中。挂载的运行时配置也接受同一个表；完整字段、默认值、容量语法和环境变量覆盖见[完整示例](../../examples/full.toml)。历史查询、存储模式、持久化，以及配置变更所需的容器重启，请参阅[查看和保留日志](runtime.zh-CN.md#查看和保留日志)。

## 选择应用和工具功能

Manager 和 comfy-cli 是分别独立控制的可选功能。省略其开关时，两者都启用；也可分别禁用其中任何一个：

- `comfyui.install_manager` 控制来自所选 ComfyUI 检出版本的 Manager 功能及其 `cm-cli` Registry 接口。Registry 自定义节点需要 Manager。启用该功能不会向 ComfyUI 运行时参数添加 `--enable-manager`。
- `comfyui.install_cli` 控制单独解析的、面向用户的 comfy-cli 工具。cdh 不使用 comfy-cli 构建镜像或安装 Registry 节点。

`python.uv_tools` 中的条目用于请求额外的隔离命令行工具。这些工具不会把软件包安装到 ComfyUI 应用环境中。有关软件包来源、解析和工具环境的行为，请参阅[构建与锁定指南](build-and-lock.zh-CN.md)。

`python.extra_packages`、`python.uv_tools` 和 `pytorch.extra_packages` 接受由 `packaging.Requirement` 解析的标准具名 PEP 508 requirement：一种是带可选 extras、标准版本 specifier 和 marker 的分发包名；另一种是写成 `name[extras] @ URL` 并可带 marker 的具名 direct reference。例如 `numpy>=2,<3`、`ruff==0.16.0rc1`，以及以下具名 wheel：

```toml
[pytorch]
extra_packages = ["sageattention @ https://github.com/jimlee2048/SageAttention/releases/download/v2.2.0/sageattention-2.2.0+cu130torch2.13-cp39-abi3-linux_x86_64.whl"]
```

cdh 接受使用 `https`、`http`、`git+https` 和 `git+http` 的具名 direct reference。URL 必须包含 host；如果编写了 port，它必须可解析；同时不得包含 username 或 password userinfo。完成这一结构检查后，direct reference 保持不透明；引用的 wheel、源码归档或 VCS 项目是否能为所选目标安装，由 uv 决定。

Marker 只针对 cdh 的固定构建目标求值一次：配置指定的 CPython 版本，以及 Linux `amd64`/`x86_64`。它绝不会使用宿主机值；不可用的 kernel release/version 值固定为空字符串。Marker 对该目标不生效的声明不会被解析或安装。需要当前不可用的软件包 metadata 或 dependency-group context 的 marker 变量（`extra`、`extras` 和 `dependency_groups`）会产生拒绝诊断。

不具名的裸 URL、本地路径和 `file:` URL、editable requirement、原始 pip/uv option、包括 `git+ssh` 在内的 SSH transport，以及包含任何 userinfo 的 URL 均不受支持。公共远程 source 必须使用 `name @ URL` 形式。cdh 不会通过自定义 selector 白名单缩窄 `packaging` 接受的标准 specifier；其中包括 compatible-release 与 exclusion clause、wildcard equality、任意相等 `===`，以及标准解析器接受的 prerelease、development 或 local-version 操作数。cdh 不会求解一般性的范围等价关系，也不会预先验证 selector 是否存在可满足的发布版本；resolver 仍必须产出 canonical PEP 440 分发包版本。在应用软件包或隔离工具中，同一规范化包的冲突生效声明会被拒绝。

## 选择自定义节点和构建 Hook

自定义节点可以使用 Registry 标识，也可以使用直接 Git URL。Registry 节点需要 Manager；直接 Git 节点不需要。混合声明会保留它们在生效配置中的顺序。在靠后的层中设置 `custom_nodes = []` 可移除继承的节点。

Git 节点在 cdh 创建目标目录并克隆仓库之前运行 `pre_clone_hooks`。锁定 commit 的 detached checkout、递归 submodule checkout 和源码身份检查通过后，`pre_install_hooks` 在 cdh 读取根目录 `requirements.txt` 或执行可选根目录 `install.py` 之前运行。可在此阶段修补节点源码或安装输入；cdh 按现有 requirements 限制消费修改后的文件。`post_install_hooks` 在安装和身份检查成功后运行。即使一个或两个可选安装文件不存在，所有已配置阶段仍然运行。Registry 的 pre/post Hook 包围 Manager 安装命令；Registry 节点不接受 pre-clone Hook。

节点及各 Hook 数组按顺序执行。任何 Hook、源码准备、安装或边界检查失败都会阻止后续工作；post-install Hook 不用于失败清理。所有构建 Hook 的工作目录均为 `COMFYUI_PATH`，`.sh` 使用 bash，`.py` 使用应用 venv Python。操作 Git 节点文件时，将配置的 `target_dir` 拼接在 `COMFYUI_PATH/custom_nodes` 下；pre-clone 必须让该目标保持不存在。Hook 内的环境或目录变更不会传回后续子进程。

Hook 路径相对于通过 `--build-hooks-dir` 显式传入的目录；不存在隐式 Hook 根目录。每个列表默认空，遵循上文的普通列表替换规则。[完整配置](../../examples/full.toml) 使用仓库中简短的 [pre-clone](../../examples/build-hooks/pre-clone.sh)、[pre-install](../../examples/build-hooks/pre.sh) 和 [post-install](../../examples/build-hooks/post.sh) 示例。Hook 在合并后的自定义节点构建指令内执行，命中 BuildKit 缓存时可能不会重新执行。

构建 Hook 和自定义节点安装程序会在镜像构建期间执行由用户选择的可信代码。使用前请检查这些代码；不要在 Hook 文件中放置机密信息，因为其内容会保留在镜像及其各层中。

## 在镜像构建期间添加文件

每个构建文件都要显式选择 HTTP 或宿主机本地来源。两种 variant 使用相同的 `source + target` 操作形状：

```toml
[[files]]
type = "http"
source = "https://example.test/model.safetensors"
target = "models/checkpoints/remote-model.safetensors"
downloader = "httpx"

[[files]]
type = "local"
source = "artifacts/model.safetensors"
target = "models/checkpoints/local-model.safetensors"
content_lock = false

[[files]]
type = "local"
source = "artifacts/workflows"
target = "user/default/workflows"
content_lock = false
```

HTTP 文件还可以选择 `checksum` 和 `download_mode`。本地来源可以是一个普通文件，也可以是一个完整的真实目录树；本地来源不使用 downloader，也不接受手写 checksum。文件 target 是确切的最终路径。目录 target 是目标根目录，cdh 会把来源目录的内容复制到该根目录下，不附加来源目录名。目录应用采用覆盖合并：选中的条目替换目标中兼容的条目，而无关的镜像下层内容保留；cdh 不做镜像同步删除。所有构建文件对其选中的条目都是权威的，因此构建配置没有 `overwrite` 字段。

本地 `source` 不能为空。相对的本地 `source` 统一以第一个 `-f` 配置文件的真实父目录作为基准；显式的 `source = "."` 会选择第一个配置文件的真实父目录本身。绝对路径和规范化后的父目录穿越均可使用。render/build 准入会选择所有真实的后代目录和普通文件，包括点开头的条目和空目录，并拒绝链接、Windows junction 或其他 reparse point、特殊文件、不可读取的条目和不安全名称。source 与渲染输出不能重叠。本地 source 路径属于普通的宿主机输入，而不是 Secret 值。`host validate` 只检查结构和 target 语法，不读取本地 source。

选中的目录和普通文件在镜像中使用 cdh 规定的 `0755` 和 `0644` 权限；宿主机所有者、时间戳、ACL、xattr 和可执行位不会被复制。

完整目标路径和选中的本地目录成员路径中，任何一段名称都不能是 `.cdh-staging`，也不能以 `.wh.` 开头。前者保留给 HTTP 下载暂存，后者保留给容器镜像的删除标记。请修改有问题的目标或成员名称；cdh 不会自动跳过它们。HTTP/local、构建/运行时配置使用同样的目标规则，包括生效的 `COMFYUI_PATH` 前缀。来源路径或 URL 不等同于目标名称：例如，本地文件 `.wh.model` 仍可复制到 `models/model.bin`。

空的本地目录有效，并仍会创建目标根目录，但 render、build、check 和 dry-run 各产生一条 warning：`local source directory is empty; its target directory will still be present in the image`。validate 不产生该 warning，quiet 模式也会保留它。

`content_lock = false` 是默认值，普通规划不会对本地字节计算内容摘要。使用 `content_lock = true` 时，cdh 会为所选本地文件或目录树内容计算内容摘要，并在准备上下文及最终镜像观测时验证该摘要。`--locked` 会检查现有 lock、本地目录树结构和启用内容锁的内容摘要，但不会比较未锁定 source 的字节；如需流式比较来源/上下文的字节相等性，请使用 `--check`。[构建与锁定指南](build-and-lock.zh-CN.md#构建文件与本地上下文-materialization)说明 materialization mode、读取和远程 builder 传输成本、空 source warning 以及镜像放置行为。本地 source 路径仅在宿主机准备阶段使用；准备好的内容仍是镜像构建输入，只有 HTTP 文件声明会成为固化的运行时默认配置。

## 为 HTTPX 文件下载提供认证

定义一个具名 Secret，再从 Bearer credential route 引用它。token 始终是对完整 Secret 值的引用，不接受 inline 值：

```toml
[secrets.hf_read]
env = "HF_TOKEN"

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

`match` 使用精确 scheme、大小写规范化后的 host、effective port 和 raw path segment 定义 credential protection space。包括 redirect 在内，每个实际 outbound request 都重新执行 longest path-segment prefix 匹配。文件 URL 的 query parameter 会原样保留，但不参与 route 选择；route 本身也不能按 query 选择 credential。redirect 离开所有 route 后不会收到 cdh Bearer 值；进入另一 route 时使用该 route 的 Secret；HTTPS route 不会授权 HTTP target，除非另有 route 明确匹配该 HTTP target。HTTP route 可以使用，但 cdh 会警告 token 缺少 TLS 传输机密性。

具名 source 使用下文 Git Secret 所述的同一宿主机 env/file acquisition 边界，上限为 65,525 bytes。请提供不含 `Bearer ` 前缀和末尾换行的裸 RFC 6750 `b64token` 值。cdh 不会 trim、Base64 decode、解析 JWT claim、要求 provider 专属前缀或在准入期间联系 provider。

认证文件的 effective downloader 必须是 `httpx`。如果初始 URL 命中 route 却选择 aria2，cdh 会拒绝配置，因为 aria2 无法执行同样的逐 redirect credential scope；普通公开下载仍可使用任一 backend。cdh 不会通过网络预检来预测公开 aria2 URL 的 redirect，因此它后来遇到受保护 endpoint 时，只会沿用 aria2 的普通 HTTP 失败路径，并且不会收到 token。

Downloader route 按 canonical `match` 合并：靠后等价 route 会在原位置原子替换完整的靠前 route，不同 route 会追加，`credentials = []` 会清除继承的 route。Secret 定义则按逻辑名称独立合并。构建 route 和 Secret source 仅用于构建，不会固化到运行时配置；需要认证运行时下载的部署必须在挂载的运行时配置中独立声明 route 与容器可见 Secret source。它们各自的交付和生命周期边界见[构建与锁定](build-and-lock.zh-CN.md#经过认证的-httpx-文件下载)与[运行时](runtime.zh-CN.md#经过认证的-httpx-下载)。

## 提供私有 HTTP(S) Git 凭据

在 `[secrets.<name>]` 下定义逻辑 Secret source，然后由一个或多个 `[[cdh.git.credentials]]` route 引用这个完整值。每个 source 只能选择一个环境变量或文件；配置只包含 locator，绝不包含解析后的值。逻辑名称与环境变量名、文件名彼此独立。

```toml
[secrets.github_pat]
env = "CDH_GITHUB_PAT"

[secrets.gitlab_pat]
file = "secrets/gitlab-pat"

[[cdh.git.credentials]]
match = "https://github.com/example-private/"
username = "x-access-token"
password = { secret = "github_pat" }
```

Secret 名称和引用必须匹配 `[a-z][a-z0-9_-]{0,63}`。`env` locator 必须是有效的环境变量名。`password` 始终是结构化的完整值 Secret 引用；它不接受内联 token 或字符串插值。

环境变量和文件 source 仅在需要它们的命令中惰性解析。仅语法验证绝不会读取它们，匹配且已接受的 lock 也可能避免 provider 阶段的读取。相对文件 locator 统一以第一个 `-f` 文件的真实父目录为基准；允许绝对路径和规范化后的父目录跳转。Secret 文件必须是常规文件而非符号链接，且值不能超过 65,525 字节。在 POSIX 上，设置了 group 或 world 权限位时，cdh 会发出 warning。在 Windows 上，请自行限制源文件的 ACL；cdh 不实现通用的 Windows access audit。Git 密码还必须非空，且不能包含 NUL、回车或换行；创建 token 文件时不要留下末尾换行。

Credential route 是通用 HTTP(S) 用户名/密码 context，而不是特定 provider 对象。GitHub 或 GitLab personal access token 应放在所引用的 `password` 中。用户名必须非空；如果不想记录个人用户名，GitHub 可便利地使用 `x-access-token`，GitLab 支持使用 `oauth2`。其他凭据类型可能要求 provider 指定的用户名。建议使用只读、仅限单个仓库且会过期的 token。

`match` 使用精确 scheme、规范化大小写的 host、等价的默认端口、精确的非默认端口以及按 path segment 的前缀匹配。最长匹配 route 胜出；host 范围的 route 以 `/` 结尾。`http://` 仍可使用，但会产生 warning，因为 Basic 风格凭据没有 TLS 传输保密性。带密码的 URL userinfo 会被拒绝；仅含用户名的 userinfo 必须与选中 route 的用户名一致。URL rewrite、redirect、CA 和 proxy 行为仍归 Git 所有，因此 route 只为 Git 提供的 context 选择凭据，并不是 endpoint attestation。

对于包含任意 direct-Git 节点的构建，cdh 会让生效 route 引用的每个不同 Secret 可用于合并后的自定义节点构建步骤，使递归 submodule 可以独立选择 route。该步骤中的 Hook、节点安装程序及其他用户选择代码均属于受信任代码，能够读取、输出、转换或复制这些凭据。cdh 会让解析后的值和 source locator 避开其 lock、BuildPlan、渲染上下文、manifest、镜像元数据及自身输出；它不会沙箱化受信任代码，也不会重写任意输出。构建和手动 Buildx 行为参见[构建与锁定](build-and-lock.zh-CN.md#通过-https-访问私有-git-自定义节点)。

当配置引用 Hook 时，请将同一个根目录传给验证、渲染或构建操作：

```bash
cdh host validate \
  -f cdh.toml \
  --build-hooks-dir build-hooks
```

## 后续步骤

- [构建与锁定](build-and-lock.zh-CN.md)说明如何验证、解析、渲染并构建生效配置。
- [运行时](runtime.zh-CN.md)说明容器启动时可以覆盖哪些已烘焙设置。
