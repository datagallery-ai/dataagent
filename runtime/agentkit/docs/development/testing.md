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
| HTTP | `test_api.py` |
| 示例仍能装配 | `test_examples.py` |

`test_product_e2e.py` 里未标 `live` 的用例走脚本化的提供者，覆盖 TUI、后端、工具、Skill、子代理、Checkpoint 和重启。标了 `live` 的用例才打真实提供者。

## 真实模型

产品通路：

```bash
DATAAGENT_V2_LIVE_ENV=/absolute/path/to/.env \
  uv run --project runtime/agentkit pytest runtime/agentkit/tests/test_product_e2e.py -m live -q
```

语义客户端另有一条，默认跳过：

```bash
DATAAGENT_SEMANTIC_LIVE_ENV=/absolute/path/to/.env \
  uv run --project runtime/agentkit pytest runtime/agentkit/tests/test_semantic_live.py -q
```

再设置 `DATAAGENT_SEMANTIC_LIVE_URL` 时，才会打到已部署语义服务的只读接口。未设置时，夹具使用本地 HTTP，只验证客户端传输和注入。

凭据留在你给的 `.env` 里。不要把密钥写进测试断言或临时配置。

## TUI

```bash
npm test --workspace @datafoundry/tui
```

改 Ink 全屏光标之前，先跑 Python 套件里的 PTY 探针。若上游已经修好一行偏移，删掉补丁，不要叠第二层。
