# 配置

[English](configuration.md) | 简体中文

需要选择镜像中的软件和文件时，从这里开始。配置完成后，按[构建指南](build-and-lock.zh-CN.md)生成镜像；部署设置的调整见[运行指南](runtime.zh-CN.md)。

## 选择起始示例

将 [`minimal.toml`](../../examples/minimal.toml) 保存为 `cdh.toml`，再从带注释的字段参考 [`full.toml`](../../examples/full.toml) 中添加所需部分。完整示例含有演示路径和脚本，不能直接作为构建方案使用。默认值可查阅完整示例中的注释。

```bash
cdh host validate -f cdh.toml
```

验证无需网络或 Docker，不写入文件，也不读取本地来源和凭据。引用构建钩子时，还需传入 `--build-hooks-dir`。具体字段语法见完整示例，命令选项见 `cdh host validate --help`。

## 分层配置

将共享设置保存在 `cdh.toml`，个人调整放在第二个文件。例如，将以下内容保存为 `local.toml`：

```toml
files = []

[comfyui]
install_cli = false
custom_nodes = []
```

这会清空继承的文件和节点，禁用 comfy-cli，保留其他设置：

```bash
cdh host validate -f cdh.toml -f local.toml
```

构建时使用相同顺序的 `-f` 参数。靠后的文件优先，但不同字段的组合方式有所区别：

| 设置 | 多层组合方式 | 清空方式 |
| --- | --- | --- |
| 表和单个值 | 表递归合并；靠后的单个值替换原值 | 显式设置所需值 |
| 普通列表，包括镜像标签、钩子列表、运行参数和 SSH 公钥 | 靠后的列表整体替换原列表 | `[]`，但最终配置必须允许空列表 |
| 系统包和 Python 包列表 | 新条目追加；跨层重复的等价条目只保留一次 | `[]` |
| 自定义节点 | 按 Registry ID、精确 Git URL 或本地目标目录匹配，在原位置覆盖 | `custom_nodes = []` |
| 文件 | 按规范化后的 `target` 匹配；相同 `type` 合并字段，不同 `type` 整体替换 | `files = []` |
| 凭据规则 | 等价的 `match` 在原位置替换整条规则 | `credentials = []` |
| 命名凭据来源 | 后一层整体替换该来源，例如用 `file` 替换 `env` | 替换来源定义 |

部分列表必须保留至少一个条目，例如最终构建平台列表。同一层内的重复条目可能无效，即使跨层允许重复。

Python 包只有在完整要求规范化后相同才会去重。添加不同版本限制**不会覆盖**先前对同一个包的要求；对目标平台同时生效的冲突要求会导致验证失败。若要替换整组包，先用一层清空，再在后一层添加新列表。Registry ID 不区分大小写，但标点不同仍表示不同资源。

所有相对的本地文件、节点和凭据文件路径，都以**第一个**配置文件所在的真实目录为基准，包括后续层中的声明。

## 选择版本和软件包

CUDA、PyTorch 和 ComfyUI 版本需要显式选择。镜像目标为使用 CUDA 的 Linux `amd64`。Python 必须选择项目支持范围内的精确稳定补丁版本；当前默认值和可选形式见[完整示例](../../examples/full.toml)。

ComfyUI 可指定发布版本、受支持的版本约束、`latest`、`nightly` 或完整的小写提交号。cdh 从官方仓库解析，所选提交必须继承受支持的 v0.11.0 基线。不受支持的版本或不存在的上游镜像标签会报错，不会自动换成其他版本。

| 想添加的内容 | 配置位置 |
| --- | --- |
| 系统软件包 | `system.extra_packages` |
| ComfyUI 使用的 Python 包 | `python.extra_packages` |
| 来自 CUDA 对应 PyTorch 软件源的包 | `pytorch.extra_packages` |
| 独立命令行工具 | `python.uv_tools` |

Python 列表接受带包名的要求，例如 `numpy>=2,<3` 或 `package @ https://example.com/package.whl`，也支持可选依赖和环境条件。条件按所选 Python 版本及 Linux `amd64` 计算，不使用宿主机环境；不满足条件的要求会被跳过。

直接来源可使用公开的 HTTP(S) 或基于 HTTP(S) 的 Git 地址。不接受无包名 URL、本地路径、可编辑安装、SSH 来源、原始安装器选项或带凭据的 URL，也不能使用依赖包元数据的 `extra` 等条件变量。直接来源对构建器可见，还可能出现在镜像历史中，切勿放入令牌。下载和 Git 凭据规则不用于软件包安装。

软件源选择及编译要求见 [Python 环境和软件源](build-and-lock.zh-CN.md#python-环境和软件源)。

## 选择自定义节点和构建钩子

Manager 和 comfy-cli 默认启用，可以分别关闭：

- `comfyui.install_manager` 提供所选 ComfyUI 版本的 Manager 和 `cm-cli`。Registry 节点需要它，但启用此项不会自动添加运行参数 `--enable-manager`。
- `comfyui.install_cli` 将 comfy-cli 安装为独立用户工具。cdh 不使用它构建镜像。

| 节点来源 | 适用情况 | 安装方式 |
| --- | --- | --- |
| `registry` | 节点已发布到 Comfy Registry | Manager 安装所选版本 |
| `git` | 需要仓库中的分支、标签或提交 | 检出锁定提交和递归子模块，再处理根目录中存在的 `requirements.txt` 和 `install.py` |
| `local` | 正在开发或提供本地节点 | 复制选中的源文件，再处理同样的可选安装文件 |

[完整示例](../../examples/full.toml) 展示各类声明。节点按配置顺序安装。Git 和本地节点不要求启用 Manager 或 comfy-cli，且不能使用相同目标目录。

本地节点需要真实来源目录，以及位于 `custom_nodes` 下的安全单层目录名 `target_dir`；来源不必是 Git 仓库。只有根目录的 `.dockerignore` 会按 Docker SDK 规则过滤复制内容，嵌套的忽略文件只是普通文件。没有规则或规则为空时选择整棵目录树。被选中的链接、特殊文件、不安全名称以及来源与输出重叠都会被拒绝。选择结果为空时允许继续，但会警告。根 `.dockerignore` 会保留，启用 `content_lock` 后其内容也参与锁定。

构建钩子拥有所在构建步骤的权限，请只使用你信任的脚本：

| 阶段 | 执行时机 |
| --- | --- |
| `pre_clone_hooks` | 仅 Git 节点，在 cdh 创建目标并克隆之前 |
| `pre_install_hooks` | Git 检出或本地复制后、读取安装文件前；Registry 节点则在 Manager 安装前 |
| `post_install_hooks` | 安装及检查成功后 |

即使可选安装文件不存在，配置的钩子仍会执行。Shell 钩子用 Bash，Python 钩子用应用解释器。钩子的工作目录是 ComfyUI 目录，`install.py` 的工作目录是节点目录。脚本必须为 `.sh` 或 `.py`，路径相对于显式传入的 `--build-hooks-dir`；见[钩子来源目录](build-and-lock.zh-CN.md#钩子来源目录)。

## 在构建时添加文件

例如，将模型和工作流目录放入镜像：

```toml
[[files]]
type = "local"
source = "artifacts/model.safetensors"
target = "models/checkpoints/model.safetensors"
content_lock = false

[[files]]
type = "local"
source = "artifacts/workflows"
target = "user/default/workflows"
content_lock = false
```

| 来源 | `target` 含义 | 复制内容 |
| --- | --- | --- |
| HTTP URL | 相对于 `COMFYUI_PATH` 的精确文件路径 | 下载的文件；不会从 URL 推断文件名 |
| 本地文件 | 相对于 `COMFYUI_PATH` 的精确文件路径 | 该文件 |
| 本地目录 | 相对于 `COMFYUI_PATH` 的目标目录，允许 `.` | 来源目录内全部内容，包括隐藏项和空目录，不添加来源目录本身的名称 |

目录采用覆盖合并：镜像中无关的已有文件会保留。文件与目录类型冲突会失败。构建文件在节点安装和钩子完成后应用，覆盖节点不会重新执行安装。构建声明没有 `overwrite` 选项；[运行时下载](runtime.zh-CN.md#文件下载与持久化)有独立的覆盖策略。

本地来源必须是真实文件或目录。链接、junction、特殊文件和来源与输出重叠都会被拒绝。与本地节点不同，文件目录来源不使用 `.dockerignore`。空目录仍会创建目标，并在准备构建目录时警告。配置验证不检查来源内容。

目标路径使用 `/`，不能相互重叠，也不能包含 `..`、反斜杠、控制字符或末尾斜杠。多余的 `/` 和 `.` 会规范化为同一目标。目标路径或复制的目录成员中，任何一级名称都不能为 `.cdh-staging` 或以 `.wh.` 开头；遇到这些名称应先重命名。HTTP 和本地文件必须指向 `COMFYUI_PATH` 下的具体文件。

镜像内目录权限为 `0755`，文件为 `0644`；宿主机所有者、时间戳、ACL、扩展属性和执行位不决定镜像权限。[复制方式与内容检查](build-and-lock.zh-CN.md#本地内容与复制方式)与文件放置规则分开选择。

只有 HTTP 文件声明还会成为运行时下载默认设置。本地来源仅在构建时使用；运行时提供文件应使用部署挂载。

## 提供凭据

不要将凭据值写入 TOML。定义一个命名来源，再从规则中引用：

```toml
[secrets.github_pat]
env = "CDH_GITHUB_PAT"

[[cdh.git.credentials]]
match = "https://github.com/example-private/"
username = "x-access-token"
password = { secret = "github_pat" }
```

来源必须且只能选择 `env` 或 `file`。文件来源可写为 `file = "secrets/github-pat"`；创建不带末尾换行的普通文件，并限制权限或 Windows ACL。宿主机凭据文件不能是符号链接。值最多为 65,525 字节；Git 密码不能为空，也不能含 NUL、回车或换行。优先使用只读、限指定仓库且有有效期的凭据。

规则按协议、主机、端口和路径段前缀匹配，最长的匹配路径优先。应使用 HTTPS；HTTP 规则会警告，因为传输途中凭据不受加密保护。Git 会使用自身的 URL 改写和重定向设置；限制凭据用途时，也应检查这些设置。用户名要求请查阅凭据提供方的说明。

### 为 HTTPX 下载提供认证

私有模型可使用 Bearer 规则，并显式选择 HTTPX：

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

只提供令牌本身，不加 `Bearer ` 前缀、空白或末尾换行。HTTPX 对每次请求（包括重定向）选择规则：离开所有规则范围时移除 cdh 令牌，进入另一规则时使用它的令牌。查询参数不参与选择；HTTPS 规则不授权 HTTP 目标。

aria2 无法执行同样的凭据范围限制。初始 URL 需要认证却选择 aria2 时会被拒绝；公开 aria2 请求后来跳转到受保护端点时，也不会收到 cdh 令牌。

构建凭据不会写入运行时设置。构建访问及缓存行为见[构建凭据处理](build-and-lock.zh-CN.md#通过-https-访问私有-git-节点)，独立的容器凭据来源见[运行时凭据轮换](runtime.zh-CN.md#认证下载与凭据轮换)。钩子和安装器是可信代码，可以读取提供给其构建步骤的凭据。

## 设置镜像默认值

`[system.env]` 用于非机密的持久环境变量：

```toml
[system.env]
TZ = "Etc/UTC"
```

这些值可能出现在镜像元数据、历史或日志中。省略 `TZ` 可保留基础镜像行为，也可在启动容器时覆盖。cdh 的运行路径和软件包管理设置应使用对应的专用配置字段。

用 `[cdh.logs]` 设置日志记录默认值，见[运行日志](runtime.zh-CN.md#查看和保留日志)。运行参数、SSH 公钥、部署挂载及其更新方式见[运行指南](runtime.zh-CN.md)。
