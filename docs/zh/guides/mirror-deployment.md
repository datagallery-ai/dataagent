# Linux 自定义镜像部署

`./deploy.sh deploy` 负责安装项目依赖、构建和启动。`start` / `restart` 只使用已有环境和产物，不下载或修复依赖。下载源地址由部署环境配置，仓库不保存这些地址或改写后的锁文件。

## 先准备制品与环境

需要 Node.js ≥22.12.0、npm、uv，以及 Python 3.11 或 3.12。已有可执行文件放在当前用户的 `PATH` 即可，脚本不会重复安装。若需安装系统依赖，确保配置的 apt / dnf / yum 仓库可达。

- **Node**：优先由运维预装。如需脚本安装，导出 `DATAAGENT_NODE_PACKAGE_URL`，指向匹配系统和架构、包含 Node 与 npm 的 `.deb` 或 `.rpm` 文件。脚本用现有包管理器安装该文件，不配置 NodeSource；包的系统依赖仍由系统仓库提供。不设置时沿用公网 NodeSource。官方 tar 包应预先解压并加入 `PATH`。
- **uv**：保持预装方式，不增加自动安装链路。可以从自定义镜像下载匹配平台的官方 uv 压缩包，解压后将 `uv`（以及可选的 `uvx`）安装到用户的 `PATH`。例如把解压目录中的文件安装到 `$HOME/.local/bin/uv`，再 `export PATH="$HOME/.local/bin:$PATH"`。也可使用 PyPI 镜像上的 uv 包，按 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装。下载器、安装脚本及其后续制品都应来自可达的下载源。
- **Python**：以 Python 3.11 为例，导出 `UV_PYTHON=/opt/python3.11/bin/python3.11`。也支持 `UV_PYTHON=3.11`，由 uv 发现本地解释器。不另写解释器搜索逻辑。

使用已有解释器时，建议显式关闭 Python 下载，缺失即报错：

```bash
export UV_PYTHON=/opt/python3.11/bin/python3.11
export UV_PYTHON_DOWNLOADS=never
```

若需下载解释器，改用 uv 自带的 `UV_PYTHON_INSTALL_MIRROR`，不要设置 `UV_PYTHON_DOWNLOADS=never`。镜像需保留官方 Python 制品的日期目录及文件名；具体规则见 [uv 环境变量说明](https://docs.astral.sh/uv/reference/environment/)。

## 部署前导出镜像配置

以下为示例地址，替换后在执行部署的同一终端中导出：

```bash
# 仅缺少 Node/npm 且需要脚本安装时设置；Debian 系统替换为 .deb 文件。
export DATAAGENT_NODE_PACKAGE_URL="https://mirror.example/node/node22.rpm"

# npm install 使用的源，也可通过项目或用户 .npmrc 设置。
export npm_config_registry="https://mirror.example/npm/"

# Python 运行依赖和构建依赖使用的索引。
export UV_DEFAULT_INDEX="https://mirror.example/pypi/simple/"

```

Node 启动前需要的制品地址通过进程环境提供。手动运行 npm、uv 或 Make 时，先导出下载源和解释器配置，或使用工具的原生配置。`deploy.sh` 的项目安装阶段也会读取根 `.env`，同名进程变量优先。镜像地址和凭证由部署环境管理；认证、CA 和代理沿用 npm / uv 原有配置。

部署的 Python 安装阶段执行原生 `uv sync`，按 `pyproject.toml` 的版本约束使用 uv 的标准索引解析和安装。仓库保留 `uv.lock` 作为解析快照，避免首次解析未选装的 Spark 时额外下载大型源码包；安装不使用 `--frozen` 或 `--locked`。不会再导出临时清单、手动改写锁文件或复制项目。切换索引后正常同步即可，由 uv 更新快照。更换索引可能重新解析全部可选依赖，即使没有选装；相应元数据和制品也需要可达。镜像需提供满足项目约束的包和构建工具，不要求不同环境的全部传递依赖版本一致。

默认环境为 `.venv`。如已设置 uv 原生的 `UV_PROJECT_ENVIRONMENT`，安装与启动会使用同一路径；相对路径以 `.` 为基准。原生工具的认证、CA、代理配置继续生效。

Python 准备采用 `uv sync` 的精确同步语义：按本次选择安装依赖，移除未选中的包；默认包含开发组，不默认启用 extras。

| 用途 | Python 安装命令 |
| --- | --- |
| 开发和测试 | `uv sync` |
| 仅运行依赖 | `uv sync --no-dev` |

对于项目已声明的可选能力，`--extra <name>` 可以重复传入。每次同步都要显式传入所需 extras；不再选择时，其依赖会被移除。在 Python 安装阶段选好所需能力，再启动服务。运行和测试默认共用项目环境，测试准备包含开发组；切换依赖选择会改变同一环境。

启动入口使用已准备的解释器。直接用 uv 启动时添加 `--no-sync`，依赖变更后先按所需选择重新准备环境。

Make 通过 uv 直接调用测试编排脚本，统一准备 Python 开发依赖，再执行 pytest 和所选前端测试。环境已准备好时，可用 `make ut UV_RUN_OPTS=--no-sync` 或 `make st UV_RUN_OPTS=--no-sync` 跳过 Python 依赖准备。

<a id="staged-installation"></a>

## 分阶段安装与原生命令

在仓库根目录、同一终端中先按上文配置解释器与下载源，再执行以下阶段：

```bash
npm install --include=dev
uv sync --no-dev --extra video
npm run build
```

第二步使用已导出的变量或 uv 配置。只需要基础运行依赖时可省略 `--extra video`；需要 Spark/Hive 和视频时选择 `--extra all` 并准备相应外部环境。已有环境执行同步也会移除未选中的依赖。

然后按 [配置说明](../quick-start.md) 在仓库根目录创建并填写 `.env`，启动时显式选择这份文件：

```bash
uv run --no-sync --env-file .env dataagent serve
```

这里由 uv 将仓库根 `.env` 注入进程环境。若改用 Home `.env` 或进程环境提供模型配置，省略 `--env-file .env`。需要替换 Home 环境文件时，将 `--env-file` 放在 `dataagent serve` 之后。只提供 A2A 时将 `serve` 换成 `serve-a2a`；仅运行后端可跳过 npm 安装和前端构建。`serve-a2a` 没有 REST 健康、会话和产物接口。需要前端时，连接已有后端的命令见 [Web/TUI 连接](../quick-start.md#connect-source-frontends)。

## 模型与检查

仓库根 `.env` 中的 `LLM_BASE_URL` 改为部署环境可达的兼容模型网关，并配置对应的 `LLM_MODEL`、`LLM_API_KEY`。可从仓库根 `.env.example` 复制模板。模型网关与包镜像是不同用途的地址。

```bash
node --version
npm --version
uv --version
./deploy.sh doctor
./deploy.sh deploy --non-interactive
./deploy.sh status
```

`doctor` 显示生效的 npm registry、显式配置的镜像响应和本地 Python 发现结果。镜像只发送有超时的 HEAD 请求，不安装依赖、不下载 Python，不打印 URL 中的用户名、密码或查询参数。HTTP 响应仅说明该入口有响应；例如制品目录返回 403 / 404，不代表某个具体制品不能下载。仍需通过部署确认认证、制品内容和平台兼容性。

部署的安装诊断继续写 `backend.log`，Web / TUI 构建分别写 `web.log` / `tui.log`，均在 `<DATAAGENT_HOME>/runtime/logs/` 下，所有阶段复用这三个日志文件。

仓库可用 `npm run smoke:native-deploy` 在临时工作目录验证安装、Web/TUI 构建、启停和健康检查；它沿用上述环境变量并执行 Python 环境准备。无模型配置时 `/healthz` 应为 200，`/ready` 为 503 属于预期。本地测试验证通用流程；目标部署环境仍需使用实际镜像与机器完成端到端验收。

自定义安装变量应供安装命令和启动命令使用。解释器选择和虚拟环境更新由 uv 管理；变更 Python 选择时，uv 可能重建项目虚拟环境。自定义环境路径应只指向本项目专用的虚拟环境。TUI 启动也要求已有环境，缺失时按提示执行 `uv sync`。
