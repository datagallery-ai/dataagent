# 测试

先按[开发环境](setup.md#prepare-source)准备需要的依赖。默认测试使用合成数据和模型替身。带 `live` 标记的用例需要显式选择并提供凭据。

| 场景 | 命令 |
| --- | --- |
| 运行一个 Python 测试文件 | `uv run --no-sync pytest -q tests/test_api.py` |
| 检查 Python 代码 | `uv run --no-sync ruff check .` |
| 运行单元测试 | `make ut`，或用 `SCOPE=dataagent,api` 选择组件 |
| 运行串行系统测试 | `make st`，会先构建 TUI |
| 运行两个测试层级 | `make test`，覆盖 Python、API、Web、TUI、部署及 YAML |
| 验证文档构建和链接 | `npm run docs:build` 和 `npm run smoke:docs` |

Make 测试入口在需要 Python 时先同步一次依赖。已有环境时可传 `UV_RUN_OPTS=--no-sync`。各层级实际执行的命令可用 [测试运行器](../../scripts/run_product_tests.py) 的 `--list` 查看：

```bash
uv run --no-project python scripts/run_product_tests.py --tier test --scope dataagent --list
```

用 `PYTEST_ARGS` 传 Python 测试过滤项，用 `NODE_TEST_ARGS` 传 Node 测试参数。系统测试串行运行。`make st` 拒绝 pytest 的 `-n` 和 `--numprocesses`，`make test` 会把它们传给单元测试、从系统测试参数中移除。摘要与 JUnit 结果在 `artifacts/product/`。

直接执行 pytest 时，项目默认排除 `tui` 和 `live` 标记。需要真实终端的 `tui` 用例还要求已构建的 TUI 和 POSIX 伪终端。

## 覆盖什么

| 区域 | 测试文件 |
| --- | --- |
| 配置合并、YAML、Home 启动 | `test_config.py`、`test_config_documents.py`、`test_home_startup.py` |
| 声明和编译 | `test_declarations.py`、`test_compiler.py`、`test_compiler_contract.py` |
| 宿主模板渲染、wheel 安装后的资源读取 | `test_prompts.py`、`packaging/test_installed.py` |
| Hook | `test_hooks.py`、`test_native_hooks.py` |
| MCP | `test_mcp.py` |
| 语义配置和客户端 | `test_semantic_configuration.py`、`test_semantic_client.py` |
| 会话、产物、Trace | `test_session_isolation.py`、`test_outputs.py`、`test_traces.py` |
| 扩展刷新 | `test_agent_refresh.py` |
| HTTP | `test_api.py`、`test_product_api.py` |
| A2A 契约、诊断与取消 | `test_a2a_server.py`、`test_a2a_client.py`、`test_a2a_observability.py` |
| A2A 真实进程与重启 | `test_a2a_e2e.py`，使用本机合成提供者，不访问真实模型 |
| A2A 真实模型 | `test_a2a_live.py`，显式提供网关环境变量后运行 |
| nl2sql 主代理委派、结果文件与限额 | `test_nl2sql_plugin.py`，脚本化主模型/子模型与本机 SQLite |
| nl2sql SQL 安全与场景包 | `test_nl2sql_security_rules.py`、`test_nl2sql_scenario_assembly.py`、`nl2sql_scenarios/` |
| 后端日志、轮转与脱敏 | `test_logging.py` |
| 示例仍能装配 | `test_examples.py` |

`test_product_e2e.py` 里未标 `live` 的用例走脚本化的提供者，覆盖 TUI、后端、工具、Skill、子代理、Checkpoint 和重启。标了 `live` 的用例才打真实提供者。

`test_a2a_e2e.py` 归入产品脚本的串行系统测试。它验证真实 `serve-a2a` 进程、TCP、默认匿名访问、模型错误分类、日志落盘，以及重启后旧 Task 不存在但会话历史仍可续接。外部 HTTPS 和网关仍需在实际部署环境验收。

<a name="a2a-nl2sql"></a>

## A2A 与 nl2sql 的联合验收

协议与插件测试分别通过，不代表真实模型下的整条委派链通过。建议联合验证主代理路由、nl2sql 业务结果、会话/产物与错误关联，具体数据、用例和执行安排由测试设计。本页维护自动化入口和覆盖边界。

测试应保留主代理入口，并核对实际 `task` 委派、业务结果、CSV/meta 与诊断关联。`TASK_STATE_COMPLETED` 和查询成功的区别见 [A2A 的本地子代理约定](../zh/reference/a2a.md#inbound-and-subagents)。本地 schema 与真实语义服务的覆盖范围分别记录；可控模型测试不能替代真实环境联合验收。

<a name="package-tests"></a>

## 安装包测试

```bash
uv run --no-sync pytest -q tests/packaging
```

[安装包测试](../../tests/packaging/test_installed.py) 在临时目录构建 sdist，再验证从 sdist 构建的 wheel 可独立安装，检查 CLI、SDK 和内置资源。当前交付文件的端到端验收见[发布流程](release.md#verify-artifacts)。

<a name="live-models"></a>

## 真实模型

A2A 通路使用三个环境变量：`DATAAGENT_A2A_LIVE_BASE_URL`（包含 `/v1` 的模型网关地址）、`DATAAGENT_A2A_LIVE_API_KEY`、`DATAAGENT_A2A_LIVE_MODEL`。通过当前进程环境注入凭据后，在仓库根运行：

```bash
uv run --no-sync pytest tests/test_a2a_live.py -m live -q
```

缺少任意变量时跳过。用例启动本机匿名 `serve-a2a`，验证自动生成 context、续聊记忆、同 context 排队及检查点继承、不同 context 并发、匿名查询和取消、取消后续聊，以及响应、会话索引、trace、日志中的 ID 和错误码关联。所有调用方共享同一团队范围，不新增身份或存储实体。

本机转发器透传真实模型响应，在请求体发往真实网关后控制读取时机，使排队、取消和 `TIMEOUT` 可以稳定重现。错误用例还会向真实网关发送一次无效模型密钥，验证上游 401 映射为任务 `MODEL_ERROR`；这与 A2A 入口鉴权无关。取消验证的是本地任务终止和会话释放，不代表上游停止计算或计费。

临时 Home 的模型配置只保存 `$env{DATAAGENT_A2A_LIVE_API_KEY}` 引用。每个用例在 pytest 临时目录生成 `a2a-live-report.json`、`backend.stderr.log` 和已有的 Home 日志、检查点、trace，并检查产物没有真实密钥。报告保留 ID 和诊断路径，方便复查。除上游鉴权失败用例要求 401 外，包括取消、超时在内的真实网关请求均要求最终返回 200，避免延迟掩盖无效凭据。

用例要求错误根 span、模型子 span、错误详情、ID 关联和检查点齐全，报告中的 `modelSpanPresent` 必须为真。中断模型记录保留原生 ID、父子关系和开始时间，错误说明包含 `interrupted locally`。模型结果与 Token 用量按实际响应记录。主动取消和超时都可能表现为根 `CancelledError`，用任务中的 `metadata.dataagent.error.code`（`CANCELLED` / `TIMEOUT`）及日志区分。导出规则见[可观测性](architecture/observability.md)。

产品通路：

```bash
DATAAGENT_LIVE_ENV=/absolute/path/to/.env \
  uv run pytest tests/test_product_e2e.py -m live -q
```

语义客户端另有一条，默认跳过：

```bash
DATAAGENT_SEMANTIC_LIVE_ENV=/absolute/path/to/.env \
  uv run pytest tests/test_semantic_live.py -m live -q
```

再设置 `DATAAGENT_SEMANTIC_LIVE_URL` 时，才会打到已部署语义服务的只读接口。未设置时，夹具使用本地 HTTP，只验证客户端传输和注入。

凭据通过当前进程环境或外部 `.env` 提供。不要把密钥写进测试断言或临时配置。

## TUI

完成[开发环境](setup.md#prepare-source)准备后，用 `npm run test:tui` 编译并运行 Node 测试。与后端联调时运行 `make st`，它也会构建 TUI。Python 的 `tui` 用例需要 POSIX PTY。输入法候选窗仍要在实际终端手动验证。

改 Ink 全屏光标之前，先跑 Python 套件里的 PTY 探针。若上游已经修好一行偏移，删掉补丁，不要叠第二层。
