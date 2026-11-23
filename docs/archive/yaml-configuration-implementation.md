# JSON → YAML 实现与验证

> 历史设计与验证记录。当前版本直接使用 YAML，配置入口见[配置指南](../guides/configuration.md)。

实现日期：2026-11-18。对应设计：[配置系统从 JSON 迁移到 YAML](yaml-configuration-migration.md)。

## 修改范围

- Python：新增 `dataagent/config/strict_yaml.py`。统一读取主配置、插件清单、默认配置、子 Agent、MCP、独立 NL2SQL 参数和 UDN 维度映射。自动发现使用规范 `.yaml`，显式路径支持 `.yml`，旧 JSON 给出迁移提示。
- 严格解析：YAML 1.2 Core、单 mapping 文档、字符串键、有限数值及安全整数范围。拒绝重复键、锚点、别名、合并键、显式标签和超深结构；错误只包含路径、类别和可获得的行列。
- 启动：Home 模板初始化保持 0600 和不覆盖规则；只有旧 JSON 时不创建 YAML 遮盖旧配置；降级监听也使用严格读取。插件刷新指纹使用 YAML 文件。
- Node/TUI：直接依赖 `yaml` 2.x，复用 `ui/shared/config-yaml.mjs` 和监听读取逻辑。server 环境变量读取 Home `.env`，非空进程值覆盖文件值；不展开未使用模型的密钥。
- 部署：通过 YAML Document 局部修改 server 字段，保留注释、键顺序及占位符；同目录私密临时文件和原子替换，异常清理本次临时文件。
- 迁移：新增 `dataagent config migrate`，默认预览；追踪声明配置闭包，先完成预检，源 JSON 原样保留。等价目标跳过、不同目标报冲突；独占创建、0600、中途失败撤回本次新建文件。类型比较区分布尔与数字。
- 资源：21 份配置改为 YAML；同步清单引用、初始化模板、示例、测试、CLI 帮助和活跃文档。历史 archive 标明旧 JSON 版本。

## 使用

```sh
# 默认只预览 DATAAGENT_HOME（未设置时为默认 Home）
uv run dataagent config migrate

# 指定范围；确认预览后才加 --apply
uv run dataagent config migrate --home /path/to/home --apply
uv run dataagent config migrate --plugin /path/to/plugin --apply
uv run dataagent config migrate --file /path/to/team.json --apply
```

`--home`、`--plugin`、`--file` 可重复并组合。显式 `--file` / `--plugin` 不隐式加入真实 Home。动态环境插件路径需用 `--plugin` 补充；迁移不展开变量、不导入插件、不连接模型或数据库。完成后重启应用。

## 首轮实现验证（本次 review 修复前）

最终后端回归通过临时 DATAAGENT_HOME 运行；没有使用真实模型或付费请求。

- Python 完整离线套件：1830 项通过、30 项子测试通过，5 项跳过、10 项 live 测试排除。命令：`uv run pytest tests -m 'not live' -q --tb=short`（以临时 DATAAGENT_HOME 运行）。
- Python 共享解析与迁移专项：38 项通过；共享解析夹具共 28 项。
- `npm run test:yaml`：36 项通过，涵盖共享契约、监听读取、保留注释的写入、环境来源和旧格式检查。
- `npm run test:tui`：192 项通过，包含 TypeScript 构建。
- `npm run test:web`：67 个文件、635 项通过。
- 配置/启动/部署 CLI 定向 Node 测试：79 项通过、1 项跳过。
- 完整 Node 配置/部署套件：144 项通过、1 项跳过、3 项失败，退出码 1。此结果不能记为全通过。
- `npm run smoke:docs`：通过，检查 48 份文档。
- 修改的 Python 文件 `ruff check`：通过。`git diff --check`：通过。
- `uv build --project .`：sdist 与 wheel 构建通过。在临时目录安装 wheel 后，验证 Home YAML 初始化、内置插件默认配置和子 Agent 声明加载通过。

完整部署套件的 3 项失败在未修改的 HEAD 临时副本中也可复现：

1. `scripts/deploy/bootstrap.test.mjs` 的 help 测试：当前 macOS 没有 `/etc/os-release`，脚本要求 Ubuntu/Debian。
2. 同文件的 unsupported architecture 测试：先触发相同的操作系统检查，未到达预期架构错误。
3. `scripts/deploy/process-state.test.mjs`：测试进程收到 SIGTERM；TAP 报告 `ERR_TEST_FAILURE`。未修改版本同样失败。

未因本次 YAML 迁移修改这些部署平台行为。

## 保留的 JSON

工具链文件（npm、TypeScript、pyright）、HTTP/AG-UI/MCP/A2A 协议、ready 握手、会话与日志/trace/部署运行状态、国际化字典、业务数据集和响应夹具均保留 JSON。新增 `tests/fixtures/yaml-contract.json` 是两端共享的测试数据容器，不是产品配置。`config/strict_json.py` 仅供一次性迁移读取旧文件。

本次未迁移真实用户 Home，未重启已有应用，未提交或推送代码。


## Review 修复：4 个可复现问题

本轮只修改 child worktree，保留原有未提交实现；未修改父 worktree、未重启 Cursor、未提交或推送。

1. **dotenv 顺序与覆盖。** 新增 `ui/shared/config-env.mjs`，按文件顺序解析绑定并展开引用，不再依赖 `util.parseEnv` 返回对象的键顺序。重复键、前向引用、默认值、空值、无赋值键、引号/转义/多行、export 和 BOM 均按 python-dotenv 语义处理。展开阶段保留先前文件绑定（含保留变量）；完成后过滤保留变量及空文件值，最后叠加非空进程值。显式 env 文件替代 Home `.env`，缺失时报错。
2. **迁移字符串类型。** `ConfigDumper` 使用 `CoreLoader` 的 YAML 1.2 resolver 决定字符串和键的引用方式，修复 `008`、`0o12`、`1e2` 被输出为数字的问题；保留 Unicode 和多行块式文本。新增 8 组字符串值/键的预览零写入、apply、严格回读、源文件不变和重复迁移幂等测试。
3. **TUI 选定 env 文件。** `runTui → connectTui → normalizeRuntimeUrl/sessionTransport → configuredListen → readServerListen` 全链路传递 `envFile`。新增域名 ready URL 配合 `--env-file`、以及同时提供 `--config` 的回归；确认进入界面成功、正常退出时停止后端，跨 origin 请求仍被拒绝，fetch 仍使用 `redirect: error`。
4. **未选中的 Home 配置。** `initialize_home` 新增 `initialize_config` 开关。显式 `--config` 时仅初始化目录及 `.env`，不检查/读取旧 Home 主配置，也不创建 Home `config.yaml`。自动使用 Home 时仍报 legacy JSON 迁移提示。两条路径都验证源 JSON 不变。

### 本轮实际验证

全部启动/运行验证均指定临时 `DATAAGENT_HOME`，没有调用真实 LLM 或数据库。

| 命令/范围 | 最终结果 |
| --- | --- |
| 下方 Python 定向回归命令 | **261 passed**，2 条既有 LangGraph 弃用警告，退出码 0 |
| 下方 Node 定向回归命令 | **92 passed, 1 skipped**，0 failed，退出码 0 |
| `DATAAGENT_HOME=$(mktemp -d) npm run test:tui` | **194 passed**，含 TypeScript 构建，退出码 0 |
| 修改的 5 份 Python 文件 `ruff check` | 通过，退出码 0 |
| `git diff --check` | 通过，退出码 0 |
| `npm run smoke:docs` | 48 份文档链接检查通过，退出码 0 |

```sh
DATAAGENT_HOME=$(mktemp -d) uv run pytest   tests/test_yaml_contract.py   tests/test_json_config.py   tests/test_config_migration.py   tests/test_config.py   tests/test_launch.py   tests/test_launcher.py   tests/test_home_startup.py   tests/test_plugin_configuration.py   tests/test_mcp.py   tests/test_layering.py -q --tb=short

DATAAGENT_HOME=$(mktemp -d) node --test   ui/shared/config-yaml.test.mjs scripts/server-listen.test.mjs   scripts/stack-runtime-config.test.mjs scripts/deploy/args.test.mjs   scripts/deploy/cli.test.mjs

uv run ruff check   dataagent/config/migration.py   dataagent/bootstrap/paths.py   dataagent/bootstrap/startup.py   tests/test_config_migration.py   tests/test_launch.py
```

Node 套件包含 8 个 Python/Node 对照用例（7 组 dotenv 语义及 1 组选定 env 文件），直接比较 Python 和 Node 的解析/监听结果。

首轮新增 TUI 测试曾因 mock 参数可能为 undefined 而构建失败（退出码 2，未执行测试）；补充存在性断言后重新执行，最终 194 项通过。该失败未记为通过。

本轮四项问题无已知剩余缺陷。没有重新执行完整后端、Web 或完整部署套件；上方首轮历史结果不代表本轮重跑。完整部署套件此前的 3 项环境相关失败仍保留记录，本轮未修改它们。

### 增量复查：对照测试的跨平台 Python 启动

将 `scripts/server-listen.test.mjs` 中的 Python 启动统一到 `runPython` helper，复用 `uv run --project <agentkit> --python 3.12 python`，移除新增 8 项对照测试对 `.venv/bin/python` 的依赖。未修改业务实现。

保留临时 Home 和 stderr 失败诊断。uv 启动时继承系统环境；对照脚本在 Python 内清空环境，再从 stdin 注入受控变量，避免宿主配置影响结果，同时保留启动器所需的 Windows 系统变量。

实际增量验证：`DATAAGENT_HOME=$(mktemp -d) node --test scripts/server-listen.test.mjs`，**16 passed, 0 failed, 0 skipped**，退出码 0；日志 `/tmp/yaml-review-portability.log`。`git diff --check` 通过，退出码 0。本次在 macOS 验证，未实际运行 Windows；未重复完整 Python/TUI/Web 套件。未提交或推送。

### 合并主仓更新后的验证（2026-11-19）

合并 `Main/deepagents_refactor_new`，保留主仓新增的跨系统 health 路径检查、直接 Python 启动和部署参数变更。解决 `connect.test.ts`、`deploy/cli.test.mjs`、`smoke-native-deploy.mjs` 的冲突，并将主仓新增的部署/包安装冒烟测试及包使用文档同步到 YAML。补齐隔离测试夹具中的 YAML 模块依赖，为新增 doctor 测试指定临时 Home。

实际验证（临时 DATAAGENT_HOME）：

- `node --test ui/shared/config-yaml.test.mjs scripts/server-listen.test.mjs scripts/stack-runtime-config.test.mjs scripts/deploy/args.test.mjs scripts/deploy/cli.test.mjs`：100 passed，1 skipped，0 failed，退出码 0。
- `npm run test:tui`：196 passed，0 failed，含 TypeScript 构建，退出码 0。
- `uv run pytest tests/test_home_resources.py tests/test_launcher.py tests/test_nl2sql_plugin.py tests/test_launch.py tests/test_config_migration.py -q --tb=short`：149 passed，8 条弃用警告，退出码 0。
- 两份冒烟脚本 `node --check`、`npm run smoke:docs`（52 份文档）和 `git diff --check` 通过。

首轮 Node 测试退出码 1：两个新增 doctor 测试未隔离 Home，触发本机旧 JSON 的存在性检查，已修正；另一个 process-state 测试进程失败，与此前记录的 macOS 失败相同，未计为通过。首轮 TUI 为 195 passed、1 failed，原因是隔离夹具缺少 YAML 模块，修正后最终 196 passed。没有修改或迁移真实用户配置。完整原生部署、包安装冒烟流程、Windows 和 Web 套件未在本次合并后运行。

### PR !670 二轮 Review 修复（2026-11-19）

- P1：严格 YAML 模块按需加载 `yaml`，部署 CLI/help 的模块加载不再依赖 node_modules。deploy 的 preflight 检测 YAML 依赖，缺失时先执行 `npm ci`（跳过 postinstall），再进入配置读取；正常 install 阶段不重复安装 npm 依赖，仍准备 Python 环境。根 .env 中的安装源配置继续传入安装命令。
- P2：runTui 在本地启动首次读取监听配置前合并仓库根 .env，已有进程环境优先。显式 runtime URL 不加载根 .env；显式 env 文件仍传递给后端和连接阶段。
- 合并最新 Main/deepagents_refactor_new（含 !672），解决两处文档冲突，保留新说明和 YAML 路径。

新增回归：无 node_modules 的隔离目录中，bash deploy.sh help 成功；空 Home 和已有 YAML 配置均进入 npm ci；模拟依赖安装成功后，实际 YAML 解析器能够继续检查配置。安装通过本地夹具模拟，无联网安装。TUI 在隔离副本中运行构建后的 runTui，覆盖根 .env、进程覆盖、显式 env 文件及远程连接，后端使用 mock。

实际验证，均使用临时 DATAAGENT_HOME：

- `node --test scripts/deploy/bootstrap-yaml.test.mjs scripts/deploy/controller.test.mjs scripts/deploy/cli.test.mjs scripts/server-listen.test.mjs ui/shared/config-yaml.test.mjs`：88 passed，1 skipped，0 failed，退出码 0。
- `node --test scripts/deploy/args.test.mjs scripts/stack-runtime-config.test.mjs scripts/python-environment.test.mjs`：26 passed，0 failed，退出码 0。
- `npm run test:tui`：197 passed，0 failed，含 TypeScript 构建，退出码 0。
- `uv run pytest tests/test_nl2sql_plugin.py tests/test_launch.py tests/test_config_migration.py -q --tb=short`：113 passed，7 条弃用警告，退出码 0。
- `npm run smoke:docs`：52 份文档通过；`git diff --check` 通过。

调试阶段曾因 TUI 隔离夹具未链接 workspace node_modules，以及 npm 模拟脚本误用 CommonJS require 而失败，均修复后重跑；未把失败记录计为通过。未运行 Windows、真实模型/数据库、完整部署、全量后端/Web、wheel 构建；此前完整部署的 macOS 失败未在本轮重跑。
