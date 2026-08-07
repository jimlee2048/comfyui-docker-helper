# 配置

[English](configuration.md) | 简体中文

本指南面向为 cdh 选择和组合 TOML 输入的用户。[严格配置模型](../../src/comfyui_docker_helper/config/final_models.py) 和验证代码是机器权威；本指南说明面向用户的选择，而不重复每一个字段。

## 选择起始示例

- [`minimal.toml`](../../examples/minimal.toml) 是可运行且受支持的最小配置。
- [`full.toml`](../../examples/full.toml) 是附有全面注释的示例。它标出了必填字段并记录实际默认值。

示例中的每一个值都是该示例作出的显式选择，并不代表隐含的默认值。请复制最接近你所需镜像的示例，然后移除或更改其中的选择。

在本地验证配置，无需访问网络、使用 Docker 或执行写入：

```bash
cdh host validate -f examples/minimal.toml
cdh host validate \
  -f examples/full.toml \
  --build-hooks-dir examples/build-hooks
```

使用 `cdh host validate --help` 查看当前命令选项。

## 对配置进行分层

重复使用 `-f/--file` 可按命令行中的顺序合并 TOML 文件。表会递归合并；靠后的标量或普通数组会替换靠前的值。有三个集合会按稳定标识合并：

- `comfyui.custom_nodes` 使用 Registry ID 或直接 Git URL；以及
- `files` 使用 `dir` 加 `filename`；以及
- `cdh.git.credentials` 使用原样编写的精确 `match` 字符串。

重复的 credential `match` 会在原位置原子替换完整的靠前 route；route 字段不会逐字段合并。靠后的 `credentials = []`、`custom_nodes = []` 或 `files = []` 会重置相应集合。每个 `[secrets.<name>]` 表也是原子 source 定义，因此靠后的层可以用 `file` 替换 `env`，而不会保留旧字段。使用不同原始字符串写出的规范等价 credential context 仍是不同的合并键，随后会因重复验证而失败。所有层生成生效配置后，才会检查严格结构、唯一性和跨字段规则。

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

## 选择应用和工具功能

Manager 和 comfy-cli 是分别独立控制的可选功能。省略其开关时，两者都启用；也可分别禁用其中任何一个：

- `comfyui.install_manager` 控制来自所选 ComfyUI 检出版本的 Manager 功能及其 `cm-cli` Registry 接口。Registry 自定义节点需要 Manager。启用该功能不会向 ComfyUI 运行时参数添加 `--enable-manager`。
- `comfyui.install_cli` 控制单独解析的、面向用户的 comfy-cli 工具。cdh 不使用 comfy-cli 构建镜像或安装 Registry 节点。

`python.uv_tools` 中的条目用于请求额外的隔离命令行工具。这些工具不会把软件包安装到 ComfyUI 应用环境中。有关软件包来源、解析和工具环境的行为，请参阅[构建与锁定指南](build-and-lock.zh-CN.md)。

## 选择自定义节点和构建 Hook

自定义节点可以使用 Registry 标识，也可以使用直接 Git URL。Registry 节点需要 Manager；直接 Git 节点不需要。混合声明会保留它们在生效配置中的顺序。在靠后的层中设置 `custom_nodes = []` 可移除继承的节点。

每个节点都可以指定安装前或安装后 Hook。Hook 路径相对于通过 `--build-hooks-dir` 显式传入的目录；不存在隐式 Hook 根目录。仓库中包含简短的 [`pre.sh`](../../examples/build-hooks/pre.sh) 和 [`post.sh`](../../examples/build-hooks/post.sh) 示例。

构建 Hook 和自定义节点安装程序会在镜像构建期间执行由用户选择的可信代码。使用前请检查这些代码；不要在 Hook 文件中放置机密信息，因为其内容会保留在镜像及其各层中。

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

环境变量和文件 source 会被惰性解析。仅语法验证绝不会读取它们，匹配且已接受的 lock 也可能避免 provider 阶段的读取。环境变量值会保留精确的 POSIX 字节。相对文件 locator 统一以第一个 `-f` 文件的真实父目录为基准；允许绝对路径和规范化后的父目录跳转。文件接纳会拒绝符号链接和非常规文件，将值限制为 65,525 字节，并在设置了 group 或 world 权限位时发出 warning。Git 密码还必须非空，且不能包含 NUL、回车或换行；创建 token 文件时不要留下末尾换行。

Credential route 是通用 HTTP(S) 用户名/密码 context，而不是特定 provider 对象。GitHub 或 GitLab personal access token 应放在所引用的 `password` 中。用户名必须非空；如果不想记录个人用户名，GitHub 可便利地使用 `x-access-token`，GitLab 支持使用 `oauth2`。其他凭据类型可能要求 provider 指定的用户名。建议使用只读、仅限单个仓库且会过期的 token。

`match` 使用精确 scheme、规范化大小写的 host、等价的默认端口、精确的非默认端口以及按 path segment 的前缀匹配。最长匹配 route 胜出；host 范围的 route 以 `/` 结尾。`http://` 仍可使用，但会产生 warning，因为 Basic 风格凭据没有 TLS 传输保密性。带密码的 URL userinfo 会被拒绝；仅含用户名的 userinfo 必须与选中 route 的用户名一致。URL rewrite、redirect、CA 和 proxy 行为仍归 Git 所有，因此 route 只为 Git 提供的 context 选择凭据，并不是 endpoint attestation。

对于包含任意 direct-Git 节点的构建，cdh 会把生效 route 引用的每个不同 Secret 提供给合并后的自定义节点构建步骤，使递归 submodule 可以独立选择 route。该步骤中的 Hook、节点安装程序及其他用户选择代码均属于受信任代码，能够读取、输出、转换或复制挂载的凭据。cdh 会让解析后的值和 source locator 避开其 lock、BuildPlan、渲染上下文、manifest、镜像元数据及自身输出；它不会沙箱化受信任代码，也不会重写任意输出。构建和手动 Buildx 行为参见[构建与锁定](build-and-lock.zh-CN.md#通过-https-访问私有-git-自定义节点)。

当配置引用 Hook 时，请将同一个根目录传给验证、渲染或构建操作：

```bash
cdh host validate \
  -f examples/full.toml \
  --build-hooks-dir examples/build-hooks
```

## 后续步骤

- [构建与锁定](build-and-lock.zh-CN.md)说明如何验证、解析、渲染并构建生效配置。
- [运行时](runtime.zh-CN.md)说明容器启动时可以覆盖哪些已烘焙设置。
