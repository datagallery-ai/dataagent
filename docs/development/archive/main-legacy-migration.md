# main_legacy 到 main 的配置与使用迁移对照

本文对照 [`main_legacy@7b2abeff`][legacy] 的 Python Runtime 和 Python 终端 CLI，以及 [`main@2903da39`][current] 的 Python Runtime 和 TUI。下文的 `main` 始终指这个固定对照版本。现行安装和配置见[部署](../../zh/guides/deploy-source.md)与[配置操作](../../zh/guides/configuration.md)。[旧版 Python CLI][legacy-cli] 在终端进程内直接运行 Agent。新版可以在 Python 进程内使用 [SDK](../../zh/guides/integrate/sdk.md)，或用 [TUI](../../zh/guides/tui.md) 连接 `dataagent serve`。

## 全局配置对照

| 项目 | `main_legacy` | `main` |
| --- | --- | --- |
| 配置入口 | `python -m dataagent --config <yaml>` 和 `DataAgent.from_config(...)` 读取旧 YAML。Flex 可按 `SUITE.include` 加载 Suite | 使用 DataAgent Home 的一份产品 `config.yaml`（schema 4，省略时也是 4），或通过 `--config` / `LaunchOptions(config=...)` 指定。用 `DATAAGENT_HOME` 选择 Home |
| 模型 | 配置 `MODEL.<别名>.provider/params`。Flex 可从 `<PROVIDER>_BASE_URL` 和 `<PROVIDER>_API_KEY` 补全连接参数 | `models.<id>` 定义连接，`dataagent.model` 选默认模型。OpenAI-compatible 服务填写 `provider: openai`，模型名、API 根地址和密钥可用 `$env{NAME}` 引用 |
| 环境变量 | 旧配置加载器读取进程启动目录中的 `.env`，YAML 与工作目录可分别选择 | Python Runtime 读 Home `.env` 或显式选择的环境文件，非空进程变量优先。`--config` 只选 YAML。源码 TUI 拉起后端时，还会预读 checkout 根 `.env` |
| 工作区与数据库 | `WORKSPACE.path` 可指定工作根，`WORKSPACE.allow_path` 列出额外允许目录。SQL 连接通常来自 `DATABASE` | 输入用 `dataagent.input_workspaces`，输出用 `dataagent.output_root` 或 SDK 的 `output_dir`，数据库连接由插件配置。原先用 `dataagent.workspaces` 的早期新版 YAML 需改字段名 |
| 扩展与语义 | Flex 用 `SUITE.include`、`TOOLS` 和 `SEMANTIC_LAYER` 等配置 | 用 `plugins.enabled` 启用插件，业务参数放 `plugins.config.<id>`。宿主 MCP 放 Home 或插件根的 `.mcp.yaml`，语义 HTTP 客户端由根 `semantic` 配置 |
| 用户与会话 | Python CLI 的 `--user` 和 `--session` 决定本地会话上下文，可续接已有 `messages.json` | TUI 使用 Server 启动时选择的本地用户，默认 `default`。会话索引、Checkpoint 和绑定存于 Home 的 `runtime/`；产物默认写入 `<home>/outputs/<user>/<thread>/` |

以下示例适用于旧 Flex 配置使用 `provider: bailian` 的情况。在旧 checkout 的 `runtime/dataagent/` 下运行时，可以将连接参数放在该目录的 `.env`：

```dotenv
BAILIAN_BASE_URL=https://model.example.com/v1
BAILIAN_API_KEY=replace-with-your-key
```

模型名仍填在旧 YAML 的 `MODEL.<别名>.params.model`。使用 `provider: deepseek` 时，按该供应商的环境变量设置。在新版中，可将同一模型名、API 地址和密钥分别放入新 Home 的 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY`，再用产品 YAML 引用：

```yaml
schema_version: 4
dataagent:
  model: primary
  input_workspaces:
  - name: source-files
    path: /data/migration/inputs
    read_only: true
models:
  primary:
    provider: openai
    name: $env{LLM_MODEL}
    base_url: $env{LLM_BASE_URL}
    api_key: $env{LLM_API_KEY}
server:
  host: 127.0.0.1
  port: 8790
```

输入目录须已经存在且进程可读。不需要文件输入时，省略 `input_workspaces`。目录的只读声明表达输入约定，文件工具与 shell 仍按宿主 OS 权限执行。部署、监听和凭据详情见[配置字段](../../zh/reference/configuration.md)、[命令与环境变量](../../zh/reference/cli-and-environment.md)和[安全边界](../architecture/security-boundaries.md)。

## 使用流程对照

| 操作 | `main_legacy` | `main` |
| --- | --- | --- |
| Python 调用 | 从 [`DataAgent.from_config(...)`][legacy-sdk] 创建 Agent，通过 `await agent.chat(...)` 执行 | 先 `prepare_runtime(LaunchOptions(...))`，再 `create_agent(...)` 和 `ainvoke/astream`。Python SDK 继续支持进程内运行 |
| 终端启动 | 在 `runtime/dataagent/` 下运行 `uv run -m dataagent --config <yaml>`。配置、模型和交互都在该进程内 | 在仓库根运行 `uv run --no-sync dataagent serve --config <yaml>`，另开终端用 `npm run start:tui -- --runtime-url http://127.0.0.1:8790` 连接。源码 TUI 可用 `npm run build:tui` 构建 |
| 会话恢复 | 以同一个 `--user` 和 `--session` 重新运行 Python CLI，读取已有消息历史 | 在选定用户的 Server 上使用 `/resume <threadId>`，也可在连接时传 `--resume <threadId>`。SDK 调用通过 `configurable.thread_id` 选择会话 |
| 查看产物 | 在 `WORKSPACE.path` 指定的工作区或默认会话工作区下查看实际文件 | 在 TUI 用 `/outputs`，或直接查看已绑定的输出目录。输出与 Checkpoint 分开，新配置只影响新会话的输入与输出绑定 |

旧版 CLI 会话可以用下列命令恢复。在旧 checkout 的 `runtime/dataagent/` 中执行，将用户和会话 ID 换成已有值：

```bash
uv run -m dataagent --config config.yaml --user alice --session session-001
```

同时跑两版时，分别使用各自的 checkout、Python 环境和状态目录。新版 TUI 使用 `--runtime-url` 时只连接指定的 Server；从源码改用 `--config` 时会自己启动后端。迁移后的第一次运行创建新会话，再通过 `/resume` 验证该会话可恢复。详见 [新版 TUI 指南](../../zh/guides/tui.md)和[会话与产物](../../zh/guides/sessions-and-outputs.md)。

## 场景一：NL2SQL 查数

例子用同一只读 SQLite 数据库 `sales.sqlite` 提问“按区域统计订单数”。两边使用相同表、字段、语义库标识和问题，比较 SQL 方言、返回列、行数和产物。

### main_legacy

在独立 Python Runtime 以 [NL2SQL Agent 模板][legacy-nl2sql] 为起点，保留模型和 `CORE` 节点配置，修改下面的字段：

```yaml
AGENT_CONFIG:
  name: NL2SQL Agent
  backend: langgraph
  type: nl2sql
MODEL:
  deepseek:
    model_type: chat
    provider: deepseek
    params:
      model: your-model-name
DATABASE:
  db_id: sales
  dialect: sqlite
  config:
    path: /data/migration/sales.sqlite
SEMANTIC_LAYER:
  base_url: https://semantic.example.com
  timeout: 30
```

旧 Flex 主代理另有 [NL2SQL 子代理样例][legacy-nl2sql-flex]。它在 `TOOLS.local_functions` 注册 `nl2sql_sub_agent_tool`，在工具的 `config.llm_model` 和 `config.core_config` 配置模型与节点。该 YAML 的数据库连接来自 `DATABASE`。

把改好的独立 NL2SQL 配置另存为 `legacy-nl2sql.yaml`，在旧 checkout 的 `runtime/dataagent/` 中运行 `uv run --extra nl2sql -m dataagent --config /path/to/legacy-nl2sql.yaml`，然后输入对照问题。

### main

从 [nl2sql-entry.yaml](../../../examples/nl2sql-entry.yaml) 开始，按上文配置 `models.primary`，给新 Home 的环境文件加上 `SEMANTIC_BASE_URL`、`NL2SQL_DATABASE_PATH`。下面片段和模型配置合在同一份产品 YAML：

```yaml
dataagent:
  model: primary
  entry: nl2sql
plugins:
  enabled:
  - nl2sql
  config:
    nl2sql:
      model: primary
      database:
        db_id: sales
        dialect: sqlite
        engine: sqlite
        path: $env{NL2SQL_DATABASE_PATH}
semantic:
  base_url: $env{SEMANTIC_BASE_URL}
```

| 旧配置或行为 | 新版迁法 |
| --- | --- |
| `AGENT_CONFIG.type: nl2sql` | 用 `dataagent.entry: nl2sql` 直接运行已启用的编译型入口 |
| Flex 的 `nl2sql_sub_agent_tool` | 省略 `dataagent.entry` 并启用 `nl2sql`，保留根 Agent，让它通过 `task` 委派 |
| `DATABASE.config.path`、`DATABASE.dialect`、`DATABASE.engine` | 展平到 `plugins.config.nl2sql.database`。例如新版 GaussVector 用 `dialect: postgres` 和 `engine: gaussvector`。旧 `cloud_core` 需要重新选择已支持的连接 |
| `CORE.perceptor/generator/validator/…` 或 Flex `core_config` | 按 [NL2SQL 支持的字段](../../../dataagent/plugins/nl2sql/README.md#配置)逐项移入 `plugins.config.nl2sql`。原 `DATABASE.perceptor_type` 如为受支持的 UDN 场景，参考 [UDN 场景配置](../../../examples/config-udn.yaml) 使用 `scenario`（例如 `udn.business_twin`）。语义服务场景名另设 `semantic.semantic_scenario` |
| `SEMANTIC_LAYER` | 用根 `semantic`。服务地址、认证和超时逐项配置，请求数据库标识要与语义服务匹配 |

验证时既要看到回答或子代理 JSON，也要确认 `error` 为空。成功查询且开启结果文件时，检查当前会话产物的 `nl2sql/<query_id>.csv` 和同名 `.meta.json`，核对 SQL、列、已取行数及截断标志。入口模式与主代理模式各用新线程，避免混用形状不同的 Checkpoint。

## 场景二：de-agent 生成 SQL 交付物

用同一条数据工程需求做对照，明确源表、标识字段、时间窗和目标表，验收生成的 DDL/INSERT 文件。测试环境可先只生成文件，再单独验证 DataOps 执行链。

### main_legacy

以 [deagent_config.yaml][legacy-de-agent] 为基线。它通过已启用的 [`data_engineering` Suite][legacy-suite] 合并工具与 Hook。下面是识别旧配置时要看的片段，需保留原模板的 `MODEL`、`SCENARIO`、`TOOLS` 和 `ACTOR_LOOP` 等内容：

```yaml
AGENT_CONFIG:
  name: data_engineering_agent
  backend: langgraph
  type: react
  enable_human_feedback: false
  hitl_auto_resolve: false
MODEL:
  chat_model:
    model_type: chat
    provider: bailian
    params:
      model: your-model-name
SUITE:
  include:
  - data_engineering
DATABASE:
  db_id:
  - sales
SEMANTIC_LAYER:
  base_url: https://semantic.example.com
  timeout: 30
  verify_ssl: false
HISTORY_SQL:
  enabled: false
```

旧 `DATABASE.db_id` 列表用于校验用户指定的 `db.table`。旧交互由 `AGENT_CONFIG`、`SCENARIO.chat.human_feedback_conditions` 与 `DATA_TASK_IR` 配合控制，旧 `hitl_auto_resolve` 还可以读取评测答案。旧 Flex 的模型连接由 `MODEL.<别名>.params` 或供应商环境变量提供，见[旧运行时模型配置实现][legacy-flex-model]。

把改好的 Suite 配置另存为 `legacy-de-agent.yaml`，在同一旧 Runtime 目录运行 `uv run --extra nl2sql -m dataagent --config /path/to/legacy-de-agent.yaml`。以准备好的测试源表验证交付过程。

### main

从 [examples/de-agent.yaml](../../../examples/de-agent.yaml) 开始，合入自己的模型地址和以下场景参数。保持 `dataagent.entry` 省略，保留负责工作流编排的根 Agent。

```yaml
models:
  primary:
    provider: openai
    name: $env{LLM_MODEL}
    base_url: $env{LLM_BASE_URL}
    api_key: $env{LLM_API_KEY}
semantic:
  base_url: $env{SEMANTIC_BASE_URL}
  timeout_seconds: 30
plugins:
  enabled:
  - de-agent
  config:
    de-agent:
      model: primary
      allowed_databases:
      - sales
      dataops:
        enabled: false
```

| 旧配置或行为 | 新版迁法 |
| --- | --- |
| `SUITE.include: [data_engineering]` 以及随 Suite 合并的工具与 Hook | 启用 `de-agent`。它的 [插件清单](../../../dataagent/plugins/de-agent/.plugin.yaml) 自动依赖 `nl2sql`，部署参数放在 `plugins.config.de-agent`。清单的 [默认配置](../../../dataagent/plugins/de-agent/config.yaml) 已提供 de-agent 需要的 NL2SQL Hive SQL 生成链和工作流 Hook |
| `MODEL.chat_model`、`SEMANTIC_LAYER.timeout/verify_ssl` | 统一模型别名。改用非 `primary` 别名时一起核对 `dataagent.model`、`plugins.config.de-agent.model` 和 `plugins.config.nl2sql.model`。语义客户端改用根 `semantic.timeout_seconds`、`semantic.tls.verify_server` 以及对应证书字段 |
| `DATABASE.db_id: [ … ]` | 将表名校验允许列表迁到 `plugins.config.de-agent.allowed_databases`。它与独立 NL2SQL 查数使用的一个 `database.db_id` 不同 |
| `DATA_TASK_IR` 业务选项、`enable_human_feedback`、`enable_ir_hitl` 和自动回答 | 仍支持的 IR 业务选项放 `plugins.config.de-agent.data_task_ir`，如 `semantic_check_policy`、`enable_ir_caliber_review`。HITL 由插件的 `hitl.policies` 注册、`params` 参数化，可在 TUI 中回答，或由 Python 调用方处理 interrupt。旧自动答复及开关不直接迁移。见 [de-agent 人工交互迁移](../../zh/guides/integrate/extensions/de-agent-hitl.md#migrate-legacy-settings) |
| `HISTORY_SQL.enabled`、旧交付守卫 | 历史 SQL 对应 `plugins.config.de-agent.history_sql.enabled`，可用 [history-sql 命令](../../zh/reference/cli-and-environment.md#dataagent-history-sql)准备语料。成对交付用 `plugins.config.de-agent.delivery.require_sql_pair`，任务预算用同组的 `task_wall_budget_seconds` 和 `max_model_calls`；按场景启用 |
| 试算或数据验证 | 验收 SQL 文件时保持 `dataops.enabled: false`。需要真实数据验证时另配 DataOps 并明确批准执行范围，再调用当前的 `dataops_validate_data` 工具 |

迁移自定义 HITL 条件时，把面向模型的说明放进清单 `hitl.instructions` 指向的 Markdown；确定性条件放进对应策略的处理器和 `params`。保留内置插件时，在其清单上维护这些变更，再用配置测试确认启用的声明。

验证链路：在一个新会话查看 `task(metadata-recall)` 和 `task(nl2sql)`，确认读取当前语义后端的证据并生成约定交付文件，例如 `sql/create_<db.table>.sql` 和 `sql/insert_<db.table>.sql`。若场景需要人工确认，另开一个含歧义的任务，确认能在同一会话回答并恢复。对照[插件说明](../../../dataagent/plugins/de-agent/README.md)检查证据及产物结构。模型回复完成或外层 Task 完成只是执行状态，还应核对 SQL、口径和实际文件。

## 迁移步骤与验收

1. **保留旧环境。** 记录旧 Runtime 使用的 YAML、模型别名、数据库和语义端点。备份配置、工作区、会话记录和产物，导出需要迁移的业务文件。凭据从现有的凭据管理重新注入。迁移验证使用新 Home 和新会话。
2. **在新环境重建公共配置。** 装好新版依赖，设置新的 `DATAAGENT_HOME`，从 [config.example.yaml](../../../config.example.yaml) 配模型、服务、必要的输入目录和插件。先用基础配置通过健康和最小提问，再把业务插件加进去。改场景配置后重启并开新会话。
3. **逐个验证场景。** NL2SQL 先用只读库、已知列和行数验证查询。de-agent 先只生成 SQL，核对证据、DDL/INSERT 和人工确认，再验收依赖真实外部服务的阶段。
4. **切换入口并保留回退。** 将进程内调用改用新版 SDK，终端使用新版 TUI 连接已验证的 Server。需要回退时，使用留存的旧 checkout、环境、YAML 和会话参数启动原 Python CLI。

### 旧版基线

从旧 checkout 的 `runtime/dataagent/` 运行。`config check` 检查配置与环境依赖，`--dryrun` 输出合并结果，只用于样例或已脱敏配置：

```bash
uv run -m dataagent config check dataagent/agents/nl2sql/nl2sql_agent.yaml
uv run -m dataagent --config dataagent/core/flex/examples/deagent_config.yaml --dryrun
uv run --extra test pytest -q tests/ut/test_cli_config_dryrun.py \
  tests/ut/interface/test_cli_session_runs.py tests/ut/suite/test_de_agent_suite_hook_wiring.py
```

旧 NL2SQL 的真实查询入口见[独立 Runtime 场景测试][legacy-nl2sql-test]。该脚本依赖已配置的模型、语义服务和数据库。

### 新版基线

设置好新 Home 与 Bash 终端环境后，从 `main` 仓库根目录运行。将下方 `config.yaml` 替换成当前要验证的那份产品 YAML。第一条命令预检配置、变量和插件声明，第二条启动控制面：

```bash
uv run --no-sync python -c 'import sys; from pathlib import Path; from dataagent import LaunchOptions, prepare_runtime; prepare_runtime(LaunchOptions(config=Path(sys.argv[1]))); print("configuration loaded")' config.yaml
uv run --no-sync dataagent serve --config config.yaml
```

另开终端按所选 Server 的监听地址检查。两条都应返回 200；`/healthz` 为 200 而 `/ready` 为 503 时，先查 Home 的 `runtime/logs/backend.log`。

```bash
curl -fsS http://127.0.0.1:8790/healthz
curl -fsS http://127.0.0.1:8790/ready
```

在另一个终端运行 `npm run build:tui` 后，用 `npm run start:tui -- --runtime-url http://127.0.0.1:8790` 接入。发起一轮对话，再验证 `/resume` 可恢复会话、`/outputs` 可找到已生成的文件。客户端回归测试可用 `npm run test:tui`。

不依赖真实模型的现行检查：

```bash
uv run --no-sync pytest -q -m 'not live' \
  tests/test_config.py tests/test_product_api.py tests/test_plugin_configuration.py \
  tests/test_nl2sql_plugin.py::test_compiled_entry_runs_nl2sql_and_writes_the_session_file \
  tests/test_nl2sql_plugin.py::test_task_end_to_end_on_sqlite \
  tests/test_de_agent_contracts.py tests/test_de_agent_ir_question_guard.py \
  tests/test_de_agent_delivery_guards.py
```

[de-agent 实际链路测试](../../../tests/test_de_agent_live.py)需要测试源表、可用模型和语义服务。为测试进程设置 `DATAAGENT_DE_AGENT_LIVE_ENV`（包含三项 `LLM_*` 的环境文件路径）与 `DATAAGENT_DE_AGENT_LIVE_URL`（语义服务根地址），再运行：

```bash
uv run --no-sync pytest -q -m live tests/test_de_agent_live.py
```

该测试生成文件并核对委派和交付，测试目标 SQL 保持为文件。

[legacy]: https://gitcode.com/datagallery/dataagent/tree/7b2abeff781b7e5449af2875df1a1c5c1761ba37
[current]: https://gitcode.com/datagallery/dataagent/tree/2903da392a341c12267f6f90977e8a03eb55e795
[legacy-cli]: https://gitcode.com/datagallery/dataagent/blob/7b2abeff781b7e5449af2875df1a1c5c1761ba37/runtime/dataagent/dataagent/interface/cli/main.py
[legacy-sdk]: https://gitcode.com/datagallery/dataagent/blob/7b2abeff781b7e5449af2875df1a1c5c1761ba37/runtime/dataagent/dataagent/interface/sdk/agent.py
[legacy-flex-model]: https://gitcode.com/datagallery/dataagent/blob/7b2abeff781b7e5449af2875df1a1c5c1761ba37/runtime/dataagent/dataagent/core/flex/flex_runtime_from_config.py
[legacy-nl2sql]: https://gitcode.com/datagallery/dataagent/blob/7b2abeff781b7e5449af2875df1a1c5c1761ba37/runtime/dataagent/dataagent/agents/nl2sql/nl2sql_agent.yaml
[legacy-nl2sql-flex]: https://gitcode.com/datagallery/dataagent/blob/7b2abeff781b7e5449af2875df1a1c5c1761ba37/runtime/dataagent/dataagent/core/flex/examples/nl2sql_flex_e2e_subagent.yaml
[legacy-de-agent]: https://gitcode.com/datagallery/dataagent/blob/7b2abeff781b7e5449af2875df1a1c5c1761ba37/runtime/dataagent/dataagent/core/flex/examples/deagent_config.yaml
[legacy-suite]: https://gitcode.com/datagallery/dataagent/blob/7b2abeff781b7e5449af2875df1a1c5c1761ba37/runtime/dataagent/dataagent/core/suite/builtin_suites/de_agent/suite.yaml
[legacy-nl2sql-test]: https://gitcode.com/datagallery/dataagent/blob/7b2abeff781b7e5449af2875df1a1c5c1761ba37/runtime/dataagent/tests/e2e/test_nl2sql.py
