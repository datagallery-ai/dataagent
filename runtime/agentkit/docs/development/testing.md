# 测试

默认套件不访问真实模型。测试自己设置 Home 和输入目录，使用合成数据。

```bash
uv run --project runtime/agentkit pytest runtime/agentkit/tests -q -m 'not live'
```

`pytest` 配置在 `runtime/agentkit/pyproject.toml`：`asyncio_mode = auto`，标记 `live` 表示需要显式提供的真实模型凭据。

## 覆盖什么

| 区域 | 测试文件 |
| --- | --- |
| 配置合并、JSON、Home 启动 | `test_config.py`、`test_json_config.py`、`test_home_startup.py` |
| 声明和编译 | `test_declarations.py`、`test_compiler.py`、`test_compiler_contract.py` |
| Hook | `test_hooks.py`、`test_native_hooks.py` |
| MCP | `test_mcp.py` |
| 语义配置和客户端 | `test_semantic_configuration.py`、`test_semantic_client.py` |
| 会话、产物、Trace | `test_session_isolation.py`、`test_outputs.py`、`test_traces.py` |
| 扩展刷新 | `test_agent_refresh.py` |
| HTTP | `test_api.py`、`test_product_api.py` |
| A2A 契约、诊断与取消 | `test_a2a_server.py`、`test_a2a_client.py`、`test_a2a_observability.py` |
| A2A 真实进程与重启 | `test_a2a_e2e.py`，使用本机合成提供者，不访问真实模型 |
| A2A 真实模型 | `test_a2a_live.py`，显式提供网关环境变量后运行 |
| 示例仍能装配 | `test_examples.py` |

`test_product_e2e.py` 里未标 `live` 的用例走脚本化的提供者，覆盖 TUI、后端、工具、Skill、子代理、Checkpoint 和重启。标了 `live` 的用例才打真实提供者。

`test_a2a_e2e.py` 归入产品脚本的串行系统测试。它验证真实 `serve-a2a` 进程、TCP、默认匿名访问、模型错误分类、日志落盘，以及重启后旧 Task 不存在但会话历史仍可续接。外部 HTTPS 和网关仍需在实际部署环境验收。

## 真实模型

A2A 通路使用三个环境变量：`DATAAGENT_A2A_LIVE_BASE_URL`（包含 `/v1` 的模型网关地址）、`DATAAGENT_A2A_LIVE_API_KEY`、`DATAAGENT_A2A_LIVE_MODEL`。通过当前进程环境注入凭据后，在仓库根运行：

```bash
runtime/agentkit/.venv/bin/python -m pytest runtime/agentkit/tests/test_a2a_live.py -m live -q
```

缺少任意变量时跳过。用例启动本机匿名 `serve-a2a`，验证自动生成 context、续聊记忆、同 context 排队及检查点继承、不同 context 并发、匿名查询和取消、取消后续聊，以及响应、会话索引、trace、日志中的 ID 和错误码关联。所有调用方共享同一团队范围，不新增身份或存储实体。

本机转发器透传真实模型响应，在请求体发往真实网关后控制读取时机，使排队、取消和 `TIMEOUT` 可以稳定重现。错误用例还会向真实网关发送一次无效模型密钥，验证上游 401 映射为任务 `MODEL_ERROR`；这与 A2A 入口鉴权无关。取消验证的是本地任务终止和会话释放，不代表上游停止计算或计费。

临时 Home 的模型配置只保存 `$env{DATAAGENT_A2A_LIVE_API_KEY}` 引用。每个用例在 pytest 临时目录生成 `a2a-live-report.json`、`backend.stderr.log` 和已有的 Home 日志、检查点、trace，并检查产物没有真实密钥。报告保留 ID 和诊断路径，方便复查。除上游鉴权失败用例要求 401 外，包括取消、超时在内的真实网关请求均要求最终返回 200，避免延迟掩盖无效凭据。

一个 span 是一次操作的追踪记录；根 span 记录整轮执行，模型子 span 记录其中一次模型调用。当前锁定的 `langchain-core 1.6.1` 在非流式 `agenerate()` 等待被取消时，会跳过模型结束/失败回调；超时同样通过取消触发。本地 tracer 在异常根执行收尾时，复用已有的未结束模型 Run 补上本地中断记录，再沿原有链路导出。已结束的模型记录和普通工具内部模型的过滤规则保持原有行为。

用例要求错误根 span、模型子 span、错误详情、ID 关联和检查点齐全，报告中的 `modelSpanPresent` 必须为真。中断模型记录沿用原生 ID、父子关系和开始时间，以根执行结束时间记录本地等待截止点，错误说明包含 `interrupted locally`。未收到的模型结果为空，Token 用量保持未知；这些信息不代表上游停止计算或计费。两种路径的根错误都可能显示 `CancelledError`，区分主动取消和超时须结合任务中的 `metadata.dataagent.error.code`（`CANCELLED` / `TIMEOUT`）及对应执行日志。

产品通路：

```bash
DATAAGENT_LIVE_ENV=/absolute/path/to/.env \
  uv run --project runtime/agentkit pytest runtime/agentkit/tests/test_product_e2e.py -m live -q
```

语义客户端另有一条，默认跳过：

```bash
DATAAGENT_SEMANTIC_LIVE_ENV=/absolute/path/to/.env \
  uv run --project runtime/agentkit pytest runtime/agentkit/tests/test_semantic_live.py -q
```

再设置 `DATAAGENT_SEMANTIC_LIVE_URL` 时，才会打到已部署语义服务的只读接口。未设置时，夹具使用本地 HTTP，只验证客户端传输和注入。

凭据通过当前进程环境或外部 `.env` 提供。不要把密钥写进测试断言或临时配置。

## TUI

```bash
npm run test:tui
```

改 Ink 全屏光标之前，先跑 Python 套件里的 PTY 探针。若上游已经修好一行偏移，删掉补丁，不要叠第二层。
