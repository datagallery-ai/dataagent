# 主包与 UI 包发布方案

状态：仓库布局已实现；统一 UI 启动器与两包发布流程仍为目标设计。更新日期：2026-11-19。代码基线：`7607e02b`。

产品对外发布两个包：Python 主包提供 Agent、API 和插件；npm UI 包同时提供 Web 和 TUI。用户安装成品后运行命令，不需要克隆仓库、编译前端或准备源码目录。两个包通过 HTTP API 和 AG-UI 事件通信，可以装在同一台机器，也可以分开部署。

本文约定包边界、安装启动、构建、兼容和验收要求。下文新增的包名、命令和接口都是目标契约，不能当作当前版本的使用说明。当前使用方式仍以[命令与环境变量](../reference/cli-and-environment.md)和[发布检查](release.md)为准。

## 仓库布局与迁移顺序

仓库根目录就是 Python 主包工程，不加中间层；UI 工程放在 `ui/`。

```text
dataagent/
├── pyproject.toml             # datagallery-dataagent → wheel
├── uv.lock
├── dataagent/                 # import dataagent；包根就是公开 SDK
│   ├── __init__.py
│   ├── client.py  host.py  lowlevel.py
│   ├── config/                # 配置声明、校验和插件清单
│   ├── model_stream.py        # 合并流式模型消息，供插件和 SDK 调用方使用
│   ├── assembly/              # 装配会话图及其上下文
│   │   └── prompts/           # 宿主提示词模板与渲染
│   ├── diagnostics/           # 结构化异常、脱敏和日志输出
│   ├── core/                  # 冻结，内容不动
│   ├── bootstrap/             # 读取启动输入，准备运行环境
│   ├── extensions/
│   │   └── a2a.py             # 远端 Agent 客户端，与 mcp.py 对称
│   ├── semantic_client/       # 语义服务客户端
│   ├── plugins/               # 内置插件与资源
│   ├── server/                # REST + AG-UI
│   │   └── a2a/               # A2A 服务端
│   ├── cli.py                 # dataagent 命令
│   └── __main__.py            # python -m dataagent
├── tests/                     # Python 测试；packaging/ 验证安装成品
├── examples/
├── ui/
│   ├── package.json           # @datagallery/dataagent-ui；发行入口后续实现
│   ├── launcher/              # 后续实现命令、版本检查、后端进程协调
│   ├── shared/                # 日志、脱敏、监听地址、解释器路径
│   ├── web/
│   └── tui/
├── scripts/                   # 仅开发、发布、部署工具
├── docs/                      # 一棵文档树
├── package.json               # private；开发 workspace 根
└── Makefile  mkdocs.yml  README.md
```

`datagallery` 只表示组织；`dataagent` 表示产品、导入包和主命令；`ui` 表示界面包；`dataagent-ui` 表示 UI 发行包与命令。移除 Python 工程的 `runtime/agentkit` 中间层，不使用 Python `src/` 布局。UI 工程内部仍可保留自己的 `src/`。

在根目录执行 `pip install .`、`uv run pytest`、`uv run dataagent serve`。开发使用 uv 可编辑安装；`tests/packaging` 从仓库外安装成品，覆盖本地源码遮蔽安装包的风险。

依赖方向为 `cli → server → SDK → 内部实现`。SDK 公开路径保持不变，包括 `dataagent.prepare_runtime`、`dataagent.host`、`dataagent.lowlevel` 和 `dataagent.core.*`。A2A 客户端归入 `extensions/a2a.py`，不连带导入服务端；A2A 服务端归入 `server/a2a/`。wheel 只安装 `dataagent` 一个顶层包。插件从源码到 wheel 都位于 `dataagent/plugins/`，删除双路径查找。

执行顺序：

1. 更新本设计，将两篇发布设计文档放到根 `docs/development/`。
2. 提交一仅移动 Python 工程文件，合并测试和文档目录，不改导入。重名说明与示例保留到独立路径。
3. 提交二将服务、A2A 和插件并入 `dataagent`，修正入口、引用、构建、测试与文档导航。
4. 提交三将 `apps/` 下的 Web 和 TUI 移到 `ui/`，运行期共享代码进入 `ui/shared/`，更新 workspace。
5. 后续单独实现 UI 启动器和统一发布脚本。本次目录迁移不宣称 UI 已可独立出包。

虚拟环境含绝对路径，迁移后在根目录重建，不搬运旧 `.venv`。

## 1. 发布决策

| 项目 | 决策 |
| --- | --- |
| 主包 | Python wheel，建议发行名 `datagallery-dataagent`，保留导入名 `dataagent` 和命令 `dataagent` |
| UI 包 | npm 包，建议发行名 `@datagallery/dataagent-ui`，统一命令 `dataagent-ui` |
| Web | 保留 Next.js Node 服务和 API 代理；发布预构建产物 |
| TUI | 编译为 JavaScript，与 Web 放在同一个 npm 包中 |
| 默认启动 | 主包独立启动；UI 默认只连接，不自动启动或安装后端 |
| 便捷启动 | 显式传入 `--start-backend`，由 UI 启动已安装的主包 |
| 数据 | 存在用户 Home，不能写进 Python 或 npm 的安装目录 |
| 版本 | 首次两包同版本发布；后续按 API 兼容范围允许独立补丁发布 |
| 运行环境 | 主包 Python 3.11 或 3.12；UI Node.js 22 起，支持范围以实际平台验收结果为准 |

包名需在发布前确认归属、权限和历史用户迁移关系。本文采用上述名字描述设计，不代表这些名称已经在公共仓库可用。`0.3.0` 仅作为发布流程的示例版本。

首版不增加第三个公开的共享协议包，不捆绑 Python 或 Node.js 解释器，不实现容器交付、操作系统服务安装或自动更新。共享客户端代码可以留在同一代码仓库，构建时进入 UI 包。

## 2. 两个包的职责

| 能力 | 主包 | UI 包 |
| --- | --- | --- |
| Agent、模型调用、会话和 Checkpoint | 负责 | 经 API 使用 |
| REST、AG-UI、A2A 服务 | 负责 | Web/TUI 使用 REST 和 AG-UI |
| Python SDK、CLI、内置插件、模板 | 负责 | 不携带后端源码 |
| Web 页面、Node API 代理、客户端错误日志 | 不包含 | 负责 |
| TUI、输入交互、事件展示 | 不包含 | 负责 |
| 主包配置初始化、版本校验和存储迁移 | 负责 | 不直接改写后端配置或数据库 |
| UI 连接设置、前端日志、后端进程启动协调 | 不依赖 UI | 负责 |
| 对端协议版本检查 | 提供版本信息 | 连接前校验 |

```text
UI 包                                          主包

浏览器 → Next.js Web → HTTP / AG-UI ──────┐
                                        ├──→ Python API → Agent / 插件
终端   → TUI        → HTTP / AG-UI ──────┘                  ↓
                                                    Home / 数据服务
```

Web 的浏览器请求保持同源：访问 Node 服务，由其转发到固定的后端地址。TUI 直接访问后端。选择哪种界面不改变会话、工具或插件的业务规则。

Web 和 TUI 是同一个发行包，但不是同一个常驻进程。只启动 TUI 时，不启动 Web；只启动 Web 时，不启动 TUI。主包可独立作为 SDK/API 使用；UI 连接远端时不要求本机安装 Python。

## 3. 用户安装与首次使用

### 3.1 本机完整安装

以下命令是目标体验。建议先创建并激活 Python 3.11 或 3.12 的虚拟环境，再安装固定版本：

```bash
python -m pip install datagallery-dataagent==0.3.0
npm install -g @datagallery/dataagent-ui@0.3.0

dataagent init
```

新增 `dataagent init` 只创建 Home 中缺少的目录和配置模板，不加载模型、不监听端口、不覆盖已有配置。用户填写 Home `.env` 中的模型参数，按业务需要配置插件，然后启动：

```bash
# 终端一，保持运行
dataagent serve

# 终端二，选择 Web 或 TUI
dataagent-ui web
# 或
dataagent-ui tui
```

默认 API 地址为 `http://127.0.0.1:8790`，默认 Web 地址为 `http://127.0.0.1:3000/data-tasks`。`serve` 和 UI 命令均以前台方式运行，Ctrl+C 结束其拥有的进程。

只用主包时不安装 UI。只连接已有后端时不安装主包。用户必须拥有 npm 全局安装目录的写权限；文档不默认要求使用 `sudo npm install`。

### 3.2 一条命令启动本机服务和界面

完成安装和配置后，可以执行：

```bash
dataagent-ui web --start-backend
# 或
dataagent-ui tui --start-backend
```

UI 调用已安装的后端入口，不运行 `pip install`、`uv sync` 或源码构建。使用虚拟环境时，应激活该环境；也可以明确选择解释器：

```bash
dataagent-ui web --start-backend --backend-python /path/to/venv/bin/python
```

Windows 中该参数对应虚拟环境内的 `Scripts/python.exe`。路径作为独立参数传递，不拼接为 shell 命令。这个参数不创建环境，也不安装依赖。

### 3.3 连接远端后端

```bash
dataagent-ui web --api-url https://agent.example.com
dataagent-ui tui --api-url https://agent.example.com
```

首版只接受无路径前缀的 API origin，不接受凭据、查询参数或 fragment。`--api-url` 不填 `/api/v1/runs`。非回环地址要求 HTTPS，且不得因连接失败而自动启动本机后端。

远端连接能力不等于后端已有身份认证。当前 REST 不校验访问令牌；远程访问必须有可用的访问控制方案，不能仅因为使用 HTTPS 就直接开放给不受信任网络。若网关需要认证头，应先实现并验收 Web/TUI 一致的认证适配，再宣称支持该部署方式。

### 3.4 直接分发文件

除了包仓库，也可以交付两个成品文件：

```text
datagallery_dataagent-0.3.0-py3-none-any.whl
datagallery-dataagent-ui-0.3.0.tgz
```

用户安装方式：

```bash
python -m pip install ./datagallery_dataagent-0.3.0-py3-none-any.whl
npm install -g ./datagallery-dataagent-ui-0.3.0.tgz
```

上述 wheel 文件名以最终 wheel 标签为准，不能把本地原生库放进包后仍声明 `any`。两个产品包都可能需要下载第三方依赖，不能把这两个文件直接称为离线安装包。离线交付另行提供目标平台的 Python wheelhouse 和经过断网验收的 npm 依赖分发方案。

## 4. 命令契约

### 4.1 主包

| 命令 | 状态与职责 |
| --- | --- |
| `dataagent init` | 新增，仅初始化缺少的配置和目录 |
| `dataagent serve` | 已有，前台启动 API；不依赖 UI 或 Node |
| `dataagent serve-a2a` | 保留现有语义，不因拆包改变协议 |
| `dataagent history-sql` | 保留现有语义 |
| `dataagent --version` | 新增，从发行元数据读取实际版本 |
| `dataagent doctor` | 新增，只读检查配置、版本、插件资源和目录权限，不调用模型或写用户配置 |

保留 `serve` 的 `--config`、`--env-file`、`--workspace`、`--user` 和 `--session`。API 监听地址继续来自主包配置中的 `server.host`、`server.port`，不新增另一套端口配置来源。

### 4.2 UI 包

| 命令或参数 | 语义 |
| --- | --- |
| `dataagent-ui web` | 连接后端并启动本机 Web |
| `dataagent-ui tui` | 连接后端并进入 TUI |
| `dataagent-ui --version` | 输出 UI 包实际版本 |
| `dataagent-ui doctor` | 检查 Node、包资源、后端可达性和协议版本；不启动进程 |
| `--api-url <origin>` | 连接目标；Web 和 TUI 相同 |
| `--start-backend` | 显式启动一个由本次 UI 管理的后端 |
| `--backend-python <path>` | 仅与 `--start-backend` 一起使用 |
| `--web-host`、`--web-port` | 仅用于 `web`；默认 `127.0.0.1`、`3000` |
| `--config`、`--env-file`、`--workspace`、`--user`、`--session` | 仅与 `--start-backend` 一起使用，原义转交给主包 |
| `--resume [threadId]` | 仅用于 `tui`；沿用客户端恢复会话的含义 |

`--start-backend` 与显式 `--api-url` 或非空 `DATAAGENT_API_URL` 冲突，直接报错。启动后端模式不使用 UI 保存的连接地址，而以启动握手返回的地址为准。连接已有后端时禁止传入后端启动参数，避免让用户误以为这些参数已经作用到已有进程。

新发行入口统一使用 `--api-url`。源码 TUI 当前的 `--runtime-url` 不直接成为新包的第二套公开参数；若保留过渡别名，只在边界层将旧 runs URL 规范化为 origin，并给出弃用提示。

UI 建议统一退出码：`0` 正常结束，`1` 运行或子进程失败，`2` 参数/配置错误，`3` 后端不可达，`4` 协议不兼容。信号退出按平台惯例处理；不要依靠日志字符串判断失败类型。主包现有命令的退出码保持兼容。

## 5. 进程生命周期

### 5.1 连接模式

1. 解析 API 地址，校验格式和来源。
2. 请求版本信息接口，确认是兼容的 DataAgent 服务。
3. 检查 `/ready`，分别报告服务不可达、协议不兼容和运行时未就绪。
4. 启动 Web 或 TUI。运行时未就绪时可以显示诊断，但禁止提交对话。

连接模式绝不安装或启动后端。UI 退出只结束自己的 Node 进程；已有后端及其他客户端继续运行。

### 5.2 管理本机后端模式

1. 显式 `--backend-python` 优先，以该解释器执行 `-m dataagent`；否则从 PATH 解析 `dataagent`。
2. 使用参数数组启动 `serve --stdio-ready`，沿用工作目录及显式 Home，生成独立的 `DATAAGENT_INSTANCE_ID`。
3. 读取现有启动消息的 `protocol`、`instanceId`、`runtimeUrl`，校验身份并转换成 API origin。监听地址为通配地址时，连接目标改为相应回环地址。
4. 调用版本信息接口再次核对实例身份，再检查 `/ready`。启动消息只证明服务器已开始监听，不证明模型可用。
5. 启动界面。启动失败或超时只清理本次创建的进程，输出错误类别及日志路径。
6. UI 退出时先请求子进程正常结束，超时后只终止自己持有的进程树。后台锁由主包释放。

该模式不接管已经运行的后端。如果同一 Home 已被锁定或端口占用，启动失败，并提示使用连接模式；不能寻找任意 PID 并停止。要让 Web 和 TUI 同时长期使用同一后端，应单独运行 `dataagent serve`。

Windows 需验证控制台窗口和进程树清理；Unix 需验证信号转发。父进程异常退出的清理协议应利用持有的管道/进程句柄实现并测试，不能仅依据可复用的 PID。首版不承诺守护进程、自动重启或开机启动。

## 6. 配置和数据归属

主包继续使用现有 Home：默认 `~/.dataagent`，由进程环境 `DATAAGENT_HOME` 覆盖。已有配置不自动改写。目录和字段沿用 [Home 与工作区](../architecture/home-layout.md)。

| 位置 | 所属与规则 |
| --- | --- |
| `<home>/config.yaml`、`.env`、`.mcp.json` | 主包配置；UI 不直接写入模型或插件配置 |
| `<home>/runtime/`、`outputs/` | 主包状态与产物，升级和卸载包时保留 |
| `<home>/ui.json` | 新增可选 UI 连接设置，仅含 UI schema 与 API 地址等非机密信息 |
| `<home>/runtime/logs/web.log`、`tui.log` | 本机 UI 日志；远端模式也写在客户端本机 |
| `<home>/runtime/logs/backend.log` | 后端所在机器的日志 |
| `<home>/runtime/ui-cache/` | UI 可清理缓存；不得混入会话和用户输出 |
| `site-packages`、npm 包目录 | 只放发行文件，运行期按只读目录验收 |

连接模式的地址优先级固定为：`--api-url` > `DATAAGENT_API_URL` > `ui.json` 中的 `api_url` > `http://127.0.0.1:8790`。不另行读取主包的监听配置来覆盖这个顺序。非默认 API 端口需显式设置 UI 连接地址。

`ui.json` 的初版格式：

```json
{
  "schema_version": 1,
  "api_url": "http://127.0.0.1:8790"
}
```

Web 启动器把最终连接地址传给自己的 Node 子进程，例如映射到已有 `API_PROXY_TARGET`；它不接受浏览器请求指定任意代理目标。TUI 使用同一份地址解析代码。模型凭据由主包读取，不注入浏览器构建产物。

安装版不自动加载当前目录的 `.env`。主包继续支持 Home `.env` 和显式 `--env-file`；源码开发入口可以保留仓库根 `.env` 行为，并在文档中区分。主包和 UI 的配置不随卸载删除，数据清理必须是单独的显式操作。

Next.js 若需要写缓存，必须重定向到用户可写目录或使用经过验证的缓存适配；首版不启用依赖包目录写入的 ISR 等功能。全局 npm 安装目录不可写时，Web 仍应正常启动和处理已支持的页面。

## 7. 包内容与构建

具体命令、输入输出映射、UI 发行元数据、锁文件处理和隔离安装步骤见[主包与 UI 包出包操作手册](package-build-runbook.md)。其中明确区分当前可执行命令和待实现的统一出包脚本。

### 7.1 主包 wheel

保留现有 hatchling 构建系统。wheel 至少包含：

```text
dataagent/                 # SDK、运行时、配置模板与包内资源
dataagent/plugins/         # 插件资源，源码与安装位置一致
dataagent/server/          # REST、AG-UI 和 A2A 服务端
dataagent/extensions/a2a.py # A2A 客户端
dataagent/cli.py            # 命令入口
*.dist-info/               # 发行版本、依赖和命令元数据
```

插件中的 JSON、Markdown、SQL、脚本和其他必要资源要按实际读取路径逐项核对。用包资源 API 或安装后稳定的包内路径定位，不回退到开发仓库。包内资源只读，插件输出写 Home 或会话输出目录。

构建命令沿用工具能力，例如 `uv build --wheel --out-dir dist/release/current`。此命令只构建主包，不构建 UI。发布前验证 `Requires-Python`、入口、许可证、资源清单及 wheel 内的依赖元数据。

`uv.lock` 控制开发和 CI 环境，不会自动约束用户的 `pip install`。发行元数据必须声明真实运行依赖；CI 同时保存该版本的已验收依赖清单，并在干净环境安装 wheel。需要精确复现时提供平台匹配的 constraints，不能声称任意未来依赖解析结果都等同于发布验收环境。

当前发行名是 `datagallery-dataagent`，导入名为 `dataagent`。

### 7.2 UI npm 包

首版采用“预构建 Next.js 产物 + npm 生产依赖”的发布方式。与容器镜像不同，npm 自己负责安装 Node 依赖，不直接复制开发机的 `node_modules`。

`ui/` 作为发行入口，`ui/web/`、`ui/tui/` 为开发项目。发行暂存目录结构：

```text
package.json
npm-shrinkwrap.json
bin/dataagent-ui.mjs
dist/launcher/             # CLI、配置、版本检查、进程管理
dist/tui/                  # TUI 编译结果及共享客户端代码
dist/web/
  package.json             # 最小 Web 项目元数据
  next.config.mjs          # 可直接加载的运行配置
  .next/                   # BUILD_ID、服务端产物、静态文件及清单
  public/                  # 项目有此目录时复制
README.md
LICENSE
release-info.json
```

Web 启动器使用当前 Node 的 `process.execPath` 和本包安装的 Next CLI，执行生产模式 `next start <绝对 Web 目录>`，不查找全局 Next，不依赖调用者 cwd，不使用 `next dev`。构建和运行使用同一 Next 版本。运行配置不能依赖仓库内 TypeScript 配置文件或开发依赖。

Next、React、TUI 依赖及服务端仍会外部加载的库列入发行包 `dependencies`。合并两套依赖时检查 React 等共享版本；共享日志和协议代码进入发行产物。源码中的 `../../../scripts/...`、workspace symlink 和未发布的内部包引用不能残留为运行依赖。

构建步骤：

1. 从固定提交和锁文件安装构建依赖；执行 Web 和 TUI 构建。
2. 汇总 Web 产物、TUI、共享代码和启动器到暂存目录。
3. 生成只含运行所需依赖的发行元数据，复制与其匹配且已审核的 `npm-shrinkwrap.json`；出包时不重新解析依赖版本。
4. 排除 `.next/cache`、测试、真实 `.env`、日志、会话和本机路径等非发行内容。
5. 用 `npm pack --dry-run --json` 审核文件清单，再执行 `npm pack`。
6. 从实际 `.tgz` 安装到全新 npm prefix，分别运行 Web/TUI。

用 `files` 明确包含产物；验证隐藏目录 `.next` 确实进入 tarball。根目录的 `package-lock.json` 不能充当用户安装 npm 包的锁文件。应用型 CLI 可随包发布 `npm-shrinkwrap.json`，但仍须在各目标平台验证 optional/native 依赖的解析。

Next.js standalone 是可选的后续优化，不是首版必需条件。若使用它，应额外处理 npm 对嵌套依赖的打包规则、静态资源以及原生模块平台差异，不能只复制 standalone 目录就声称跨平台可用。

UI 的安装钩子不执行项目编译，不下载或安装主包，也不修改用户配置。第三方原生运行依赖的安装行为单独审计。发布流水线必须证明用户无需本地 C/C++ 编译器即可在承诺的平台完成安装。

## 8. 两包版本与协议兼容

产品版本、API 协议版本、配置 schema 和存储格式版本分开管理。HTTP 路径中的 `/api/v1` 不能替代完整兼容检查，包版本相同也不能掩盖协议变更。

新增 `GET /api/v1/system/info`，在运行时未就绪但 HTTP 服务可用时仍返回发行信息。建议保持已有成功响应信封：

```json
{
  "success": true,
  "data": {
    "product": "dataagent",
    "version": "0.3.0",
    "api_version": "1.0.0",
    "instance_id": "instance-identifier",
    "capabilities": ["sessions", "runs", "run-cancel", "outputs"]
  }
}
```

这些 capability 名称属于新增契约，只能报告后端实际提供的能力；此处示例不宣称当前已经存在该接口。信息中不包含密钥、本机路径或完整配置。版本读取发行元数据，不使用独立硬编码字符串。

UI 的 `release-info.json` 记录 UI 版本、API 兼容范围、必要能力和构建提交。首版示例范围为 `>=1.0.0 <2.0.0`，实际范围必须由兼容测试支持。连接行为如下：

| 情况 | 行为 |
| --- | --- |
| 协议在范围内，必要能力齐全 | 允许使用；包版本不必相同 |
| 必要能力缺失或协议不兼容 | 禁止业务请求，显示双方版本和建议安装的版本组合 |
| 信息接口 404 或结构无效 | 判为未支持版本，不以健康检查成功替代兼容判断 |
| HTTP 可达，`/ready` 返回 503 | 显示运行时未就绪及诊断入口，不显示“可对话” |
| 本机托管模式实例身份不一致 | 拒绝连接，只清理自己启动的进程 |

新增可选字段可作为兼容变更；删除字段、改变字段含义或流式事件终止语义需评估协议主版本。配置当前为 schema 4、插件声明为 schema 3，这些值不因产品版本号改变而机械升级。

两包初次同版本发布，以后允许独立补丁。发布清单记录已验收的主包/UI 组合，避免让用户盲目升级任一包到 `latest`。

## 9. 发布、升级和回退

### 9.1 发布流水线

```text
固定提交和版本
    → 测试及许可证/资源检查
    → wheel + npm tgz
    → 无源码环境安装验收
    → 发布候选版本
    → 对同一批最终产物验收
    → 上传两个包并核验
    → 发布安装说明和版本清单
```

发布由当前 GitCode 仓库对应的 CI 或受控发布作业执行。仓库现有 GitHub workflow 的检查不能直接作为该分支已验收的证据。

每次交付除两个包外，保存 `release-manifest.json`、`SHA256SUMS`、更新说明和验收记录。manifest 至少记录两包名称/版本、Git 提交、文件哈希、支持平台、Python/Node 范围、API 兼容范围以及配置/数据迁移要求。

PyPI 与 npm 无法原子发布。先验证两个目标仓库的名称和权限，再上传最终产物；两个包都可下载且校验通过后，才发布“该版本可安装”的公告。部分上传失败时保留已上传产物，修复另一侧；不覆盖已有版本，也不重新构建不同内容冒充同一已验收文件。

候选与正式包如果版本元数据不同，就是不同产物，最终产物必须重新验收。包签名或 provenance 可作为发布作业的一部分，但不能取代安装和业务验证。

### 9.2 用户升级

升级前结束活动运行并停止进程，备份 Home 与任何外置输出目录。SQLite 要在停止服务后备份，或使用数据库一致性备份方式；不能只复制正在写入的主数据库文件。

```bash
python -m pip install --upgrade datagallery-dataagent==0.3.1
npm install -g @datagallery/dataagent-ui@0.3.1

dataagent doctor
dataagent-ui doctor
```

根据版本说明执行必要的显式迁移，再启动并验证。UI 运行期间不执行自身升级；主包启动时不偷偷重写旧 schema。

回退先停止进程，再安装上一组已验收的版本。数据和配置未变化时可直接重启；若存储格式已迁移，则需恢复兼容备份或执行经过验证的逆向迁移。重新安装旧包不等于完成数据回退。

## 10. 实施分期

| 阶段 | 改动范围 | 完成条件 |
| --- | --- | --- |
| P0 主包可独立安装 | 发行命名、wheel 资源、版本入口、`init`、包内路径 | 新虚拟环境只装 wheel 即可初始化和启动 API |
| P1 UI 可独立安装 | `ui`、Web/TUI 汇总、共享代码、统一连接参数、版本接口 | 只装 tgz 即可连接已运行的后端，不需要源码和 Python |
| P2 本机便捷启动 | 后端解释器选择、握手、实例核对、信号清理、doctor | `--start-backend` 在支持平台可用，异常退出不误杀已有服务 |
| P3 正式发布 | 构建脚本、版本清单、CI、升级说明、平台验收 | 同一批 wheel/tgz 完成所有发布门禁，再对外发包 |

现有代码的主要落点：

| 位置 | 改动方向 |
| --- | --- |
| [主包元数据](../../pyproject.toml) | 发行名、版本、依赖和资源清单 |
| [主包 CLI](../../dataagent/cli.py) | 初始化、版本、诊断及稳定启动协议 |
| [产品路由](../../dataagent/server/product.py) | 发行信息接口与能力报告 |
| [Web 工程](../../ui/web/package.json) | 保留私有 workspace，输出发行产物与运行依赖 |
| [TUI 工程](../../ui/tui/package.json) | 保留私有 workspace，由 UI 包提供公开入口 |
| [TUI 后端启动](../../ui/tui/src/backend.ts) | 移除源码相对路径和发布版 `uv sync` 依赖 |
| [共享日志](../../ui/shared/logging.mjs) | 纳入包内共享代码，保持脱敏与轮转 |
| [Makefile](../../Makefile) | 明确构建与出包目标，不能继续把仅编译称为完整出包 |

实施时新增独立出包脚本，例如 `scripts/release/build-packages.mjs`，统一生成 `dist/release/`；该脚本名称是目标，不代表已有。源码开发命令可保留，但所有发行路径必须以安装后的文件结构工作。

实现阶段同步更新中英文产品安装文档、TUI 文档和 CLI 参考。设计落地前，不把这里的目标命令写成现有公开能力。

## 11. 发布验收门禁

验收对象是最终 wheel 和 tgz，不是工作区中的可运行源码。候选平台为 Linux x86_64/arm64、macOS arm64/x86_64、Windows x86_64；逐项记录实测结果，只对通过的平台承诺支持。Windows arm64 及其他平台不自动包含在承诺中。

| 场景 | 必须验证的结果 |
| --- | --- |
| 只安装主包，环境无 Node、npm、uv | 初始化、API、插件加载及 SDK 可用 |
| 只安装 UI，环境无 Python、无仓库 | Web/TUI 连接已有后端并正常显示 |
| 任意 cwd、含空格的安装路径 | 资源与命令定位正确，无源码绝对路径依赖 |
| npm 全局目录只读 | Web/TUI 运行正常，缓存、日志写到用户目录 |
| 包内容检查 | 插件、模板、Web 静态资源齐全；不含凭据、会话和开发环境 |
| 产品业务 | 模型流式对话、取消、会话恢复、输出文件与至少一个内置插件链路通过 |
| 两个 UI 同时连接 | 同一主包可供两种界面使用，退出其一不终止后端 |
| 后端未就绪 | 健康状态与业务就绪分开，不把页面可打开当作模型可用 |
| 自管后端 | 正常退出、启动失败、超时、端口占用、Home 锁冲突和父进程异常均有明确结果 |
| 协议与版本 | 兼容版本允许，不兼容版本及未知服务拒绝；握手身份不符拒绝 |
| 升级和回退 | 保留配置、插件、会话及输出；覆盖发布声明支持的迁移路径 |
| 平台依赖 | 不运行项目编译；原生依赖可安装且不需要用户配置编译器 |

真实模型和数据服务测试使用显式提供的测试配置。普通 CI 不把真实密钥写入包或日志；发布验收记录只保存脱敏结果。平台或业务测试未通过时缩小支持范围或修复后重测，不用源码环境测试替代成品测试。

## 12. 实施前需要确认的发布信息

两包边界和命令方向按本文实施；以下信息作为正式发包前的门禁，不阻碍本地开发：

- Python 发行名是否恢复 `datagallery-dataagent`，对应包仓库的所有权和旧用户迁移说明。
- npm scope `@datagallery` 的发布权限，采用公共还是私有 registry。
- 首个正式版本号及承诺支持的平台组合。
- 首版是否需要完全离线交付。需要时补充依赖包分发设计和断网验收。

## 13. 依据

- 当前实现：[主包打包配置](../../pyproject.toml)、[发布检查](release.md)、[CLI 参考](../reference/cli-and-environment.md)、[Home 布局](../architecture/home-layout.md)。
- [npm package.json](https://docs.npmjs.com/cli/v11/configuring-npm/package-json/)：发行文件、运行依赖和命令入口。
- [npm-shrinkwrap.json](https://docs.npmjs.com/cli/v11/configuring-npm/npm-shrinkwrap-json/)：可随应用发行的依赖锁文件。
- [Next.js CLI](https://nextjs.org/docs/app/api-reference/cli/next)：先构建、后以指定目录启动生产服务。
- [Next.js output](https://nextjs.org/docs/app/api-reference/config/next-config-js/output)：standalone 的能力与资源边界。

外部工具文档用于说明打包机制；具体参数及产物结构必须以项目锁定的 Next.js、npm、Python 构建工具版本验证。
