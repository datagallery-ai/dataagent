# AgentKit 包部署

AgentKit 沿用 Python 发行包名 `datagallery-dataagent`，导入名为 `dataagent`，CLI 为 `dataagent`。可构建为 wheel 和源码包，包含 API、A2A、SDK、Home 模板、提示词与内置插件。后端运行需要 Python 3.12。Web/TUI 使用 Node.js ≥22.12.0，通过现有 HTTP 接口连接；它们目前仍从前端源码构建，不包含在 Python 包中。

## 构建

构建机准备 Node.js 和 uv 后，在仓库根目录执行：

```bash
npm run package:agentkit
```

`make package` 调用同一命令。出包不编译 Web/TUI，也不要求先安装前端依赖；`npm run build` / `make build` 负责前端编译。

不使用 npm / Make 封装时，可直接执行相同的 uv 命令；此路径只需 uv 和 Python 3.12，无需 Node.js：

```bash
uv build --project runtime/agentkit --out-dir dist/agentkit
uv export --project runtime/agentkit --no-dev --all-extras \
  --no-emit-project --no-hashes --output-file dist/agentkit/constraints.txt
```

默认输出到 `dist/agentkit/`，当前生成 `datagallery_dataagent-0.1.0-py3-none-any.whl`、`datagallery_dataagent-0.1.0.tar.gz` 和 `constraints.txt`。版本来自 `runtime/agentkit/pyproject.toml`。构建器先生成源码包，再从源码包构建 wheel。导出的约束固定运行依赖及全部 extras 的版本，不包含开发测试依赖或依赖包本身；约束不会主动安装可选包。`constraints.txt` 仅固定版本，不包含哈希；它是本次出包的版本快照，不是带哈希的完整性清单。安装时从配置的索引下载，镜像设置沿用 uv/pip 的配置。

包安装可用 `UV_DEFAULT_INDEX`（uv）或 `--index-url`（pip）指定索引。约束文件不携带源码锁文件的制品 URL，因此包安装不需要改写锁文件或执行 `sed`。

wheel 和约束文件不包含第三方依赖制品，安装时仍需可达的包索引。完全离线安装还需按目标系统、架构、Python 版本与所选 extras 另行准备依赖制品。

## 安装并启动后端

将 wheel 和约束文件复制到部署目录，该目录无需项目源码。在该目录执行：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -c constraints.txt datagallery_dataagent-0.1.0-py3-none-any.whl
```

已有 pip 的虚拟环境也可使用 `python -m pip install -c constraints.txt datagallery_dataagent-0.1.0-py3-none-any.whl`；前述 `uv pip` 命令不要求目标环境安装 pip。Windows 的解释器和 CLI 位于 `.venv\Scripts\`。

`UV_PYTHON` 等环境变量供环境准备工具选择解释器；安装后的 `dataagent` 命令由所在虚拟环境的 Python 执行，不会根据这些变量切换运行中的解释器。包声明的 Python 范围仍为 3.12.x。

配置部署机器可达的模型服务，再启动安装后的命令：

```bash
export LLM_MODEL=your-model-name
export LLM_BASE_URL=https://your-openai-compatible-host/v1
export LLM_API_KEY=replace-with-your-key
.venv/bin/dataagent serve
```

首次启动会在 Home（默认 `~/.dataagent`，可通过进程变量 `DATAAGENT_HOME` 指定）创建缺少的配置模板。包运行读取进程环境、Home `.env` 或显式 `--env-file`；不会自动读取源码仓库根 `.env`。监听地址来自 `config.json` 的 `server.host` / `server.port`，默认 `127.0.0.1:8790`。也可用安装后的命令 `dataagent serve-a2a` 单独启动 A2A。

## 可选能力依赖

默认安装包含服务启动、模型对话、SQL 解析及 SQLite、MySQL、PostgreSQL 驱动。MySQL 的 `PyMySQL[rsa]` 包含 SHA-2 认证支持；PostgreSQL 使用 `psycopg2-binary`，导入名为 `psycopg2`，当前执行引擎名为 `gaussvector`，SQL 方言使用 `postgres`，完整配置见 [nl2sql 的 GV 示例](https://gitcode.com/datagallery/dataagent/blob/deepagents_refactor_new/runtime/agentkit/builtin-plugins/nl2sql/README.md#gaussvector-配置)。这些基础数据依赖随普通安装和 `uv sync` 安装，无需选择 extra。

选择 `psycopg2-binary` 是为了减少部署所需的编译环境；它自带 libpq、OpenSSL 等库，应随驱动包发布维护更新，不能仅依赖系统库升级。`PyMySQL[rsa]` 会同时安装 SHA-2 认证所需的 cryptography。

以下扩展能力在运行后端的同一个 Python 环境中按需选择：

| 能力 | extra | 安装的依赖 |
| --- | --- | --- |
| Spark/Hive | `spark` | PySpark 4.0.x |
| 视频读取、抽帧 | `video` | PyAV 16.x、Pillow 12.x |
| 上述全部扩展能力 | `all` | 引用 Spark 和视频 extra，不包含开发测试工具 |

例如，安装本地 wheel 并额外选择视频能力：

```bash
uv pip install --python .venv/bin/python -c constraints.txt \
  "./datagallery_dataagent-0.1.0-py3-none-any.whl[video]"
```

全部需要时，将安装参数改为 `"./datagallery_dataagent-0.1.0-py3-none-any.whl[all]"`。源码运行先执行 `npm run prepare:python -- --no-dev --extra video`，再用 `npm run start:api` 启动；不加 `--no-dev` 时默认包含开发组，全部功能加 `--extra all`。同步遵循 `uv sync` 的精确语义：每次显式选择所需 extras，未选中的包会被移除。直接用 uv 启动时加 `--no-sync`，避免重新解析源码锁文件。

core 中的视频错误提示沿用上游原文；若提示安装 `deepagents[video]`，本产品应安装 `datagallery-dataagent[video]`，无需另装 Deep Agents。

安装 extra 只准备 Python 依赖，不会启用插件或配置数据库。Spark/Hive 还需准备 [Spark 4.0 要求的 Java 17 或更高版本](https://spark.apache.org/docs/4.0.2/api/python/getting_started/install.html)、设置 `JAVA_HOME`，并配置相应服务。

## 一键部署源码服务

```bash
./deploy.sh deploy
```

一键部署默认选择 `--no-dev --extra video`：安装基础 MySQL、PostgreSQL 驱动和视频依赖，不安装 Spark/Hive 或 pytest、ruff 等开发工具。npm 安装阶段跳过 Python 钩子，随后通过共享准备入口同步一次；启动和重启不再次安装依赖。

普通 `npm install` / `npm ci` 的钩子仍显式选择 `--no-dev`，不启用 extras；分阶段安装可在启动前执行 `npm run prepare:python -- --no-dev --extra video`；需要 Spark 时按下述步骤显式准备。直接运行 `uv sync` 保持原生默认行为，不会自动选择 `all`。发行包的默认安装也保持基础运行依赖，完整能力使用 wheel 的 `[all]`。

需要 Spark/Hive 时，准备 Java 17 及以上与对应服务配置，再显式安装包含 Spark 的 `all`：

```bash
./deploy.sh stop
npm run prepare:python -- --no-dev --extra all
./deploy.sh start
```

直接安装独立包时选择 `[spark]` 或 `[all]`。锁定的 PySpark 源码包约 434 MB，冷安装需要下载和构建；已有可用 uv 缓存时可复用。源码首次解析或切换索引时，即使没有选装 Spark，uv 也可能下载该源码包来读取可选依赖的元数据；保留解析快照可减少这类解析。后续再次执行默认 `deploy` 会按默认选择移除 Spark，需要时重复显式准备。安装依赖不会自动配置数据库或启用插件。

<a id="connect-web-and-tui"></a>

## 连接 Web 和 TUI

前端应使用与出包代码相同的提交或已验证兼容的版本。在前端仓库中准备依赖与产物，跳过源码 Python 环境准备：

```bash
SKIP_POSTINSTALL_BUILD=1 npm ci
npm run build
```

打开另一个终端，启动 Web，使用现有 `API_PROXY_TARGET` 指向包服务：

```bash
API_PROXY_TARGET=http://127.0.0.1:8790 npm --workspace @datagallery/web run start -- --hostname 127.0.0.1 --port 3000
```

TUI 使用连接模式：

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8790
```

PowerShell 使用 `$env:NAME="value"` 设置对应进程变量。前端和后端在不同机器时，将示例回环地址换为后端可达的地址，并配置后端监听地址。

这两条前端命令只连接包服务。原来的 `npm run start`、`npm run start:api` 与 `./deploy.sh` 仍用于源码部署，会按既有规则管理仓库中的 Python 环境。`serve` 同时提供 REST 与 A2A；`serve-a2a` 只提供 A2A，Web/TUI 应连接 `serve`。

## Makefile 流水线

现有流水线可继续按下面的接口调用：

```bash
make prepare PYTHON_VERSION=3.12
make build PYTHON_VERSION=3.12
make package PYTHON_VERSION=3.12
make ut PYTHON_VERSION=3.12
make st PYTHON_VERSION=3.12
```

`PYTHON_VERSION`、`PIP_INDEX_URL`、`PIP_EXTRA_INDEX_URL`、`PIP_FIND_LINKS` 和 `NPM_REGISTRY` 保持 Make 参数兼容，已有 npm/uv 原生配置优先。安装及测试通过共享入口执行原生 `uv sync`，使用配置的索引，不要求流水线手动改写 `uv.lock`。仓库保留解析快照，但不强制冻结版本。

`build` 编译 Web/TUI；`package` 生成 Python 安装包，两个阶段不会重复编译前端。若需归档 Python 包，将 `dist/agentkit/` 中的 wheel、源码包和 `constraints.txt` 纳入产物收集；已有前端构建产物目录不变。`ut/st` 自动准备开发测试工具，不需要流水线新增安装命令。部署所需 extras 在部署阶段按需选择。

## 验证与错误定位

```bash
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
curl -I http://127.0.0.1:3000/data-tasks
```

`/healthz=200` 表示进程存活，`/ready=200` 表示会话引擎配置加载成功。随后从 Web/TUI 发送问题，确认收到流式回复；仅就绪成功不代表模型调用已验证。

未配置好模型时 `/ready` 返回 503；模型调用失败时客户端收到 `RUN_ERROR`；后端停服时 Web 代理返回 502，TUI 报连接失败。日志继续使用各进程 Home 下的 `backend.log`、`web.log`、`tui.log`。内置插件随包安装，数据库、模型和其他外部服务仍按插件配置提供。

MySQL、PostgreSQL 和 SQL 解析依赖属于基础运行依赖；缺失时应修复运行包安装。Spark/Hive 与视频抽帧通过对应 extra 安装，缺失时按错误提示和上述说明补齐。

在开发仓库运行 `npm run smoke:agentkit-package`，可验证隔离安装与上述成功、失败路径。此检查使用本地兼容模型服务，TUI 连接和流式传输通过真实客户端验证；前端绘制与真实模型服务需分别验证。
