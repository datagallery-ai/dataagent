# 构建带 NL2SQL 的数据分析 Agent

这个模式以 Deep Agents 原生主 Agent 负责规划，并给它配置一个专用的原生 NL2SQL subagent。主 Agent 可以读取文件、调用业务工具并生成报告；只有数据库问题才按需委托给 NL2SQL runnable。

## 架构

```text
用户请求
    │
    ▼
Deep Agents 主 Agent
    ├── 原生文件 / Shell / local / MCP / A2A 工具
    ├── 原生 Skills 与 Middleware
    └── 原生 subagent 委托
             │
             ▼
       NL2SQL LangGraph
       感知 → 生成 → 校验 → 执行 → 选择
```

不需要再注册单独的 NL2SQL 包装工具。`NL2SQL` YAML 段会编译成 runnable，并直接作为 subagent 交给 Deep Agents。

## 配置

```yaml
AGENT_CONFIG:
  name: "Sales Data Agent"
  description: "分析销售数据并生成有依据的报告。"
  backend: langgraph
  type: react
  primary_model: chat_model
  max_iter: 40

MODEL:
  chat_model:
    provider: deepseek
    model_type: chat
    params:
      model: deepseek-chat
      temperature: 0.1

WORKSPACE:
  path: /absolute/path/to/workspace

SCENARIO:
  chat:
    instructions: |
      把数据库问题委托给 nl2sql subagent。
      保留它生成的 SQL 和结果文件，再用业务语言解释结果。

DATABASE:
  db_id: sales
  dialect: sqlite
  config:
    path: /absolute/path/to/sales.sqlite

SEMANTIC_LAYER:
  base_url: http://localhost:32000
  username: example
  password: "123456"
  timeout: 30

NL2SQL:
  CORE:
    coordinator: {}
    perceptor:
      user_schema: null
      user_evidence: null
      user_sql_rules: sql_rules_bird
      user_few_shot_examples: null
    generator:
      strategies: [prompt]
      num_workers: 1
      num_samples: 3
    validator:
      db_explain: true
      keyword_match: false
      metadata_match: false
    reflector:
      threshold: 0.9
    executor:
      limit: -1
      preview_limit: 5
    selector:
      threshold: 0.9
```

内联配置会自动补充默认元数据：

- identifier：`nl2sql`；
- name：`NL2SQL Agent`；
- type：`nl2sql`；
- 一段指导主 Agent 委托时机的 description。

可以增加 `NL2SQL.AGENT_CONFIG` 覆盖兼容元数据，或通过 `primary_model` 选择其他已配置模型。只允许一个内联 `NL2SQL` 子 Agent。顶层 `DATABASE` 与 `SEMANTIC_LAYER` 会覆盖内联配置中的同名段，以便集中管理连接策略。

## 运行

```python
import asyncio

from dataagent import DataAgent


async def main() -> None:
    agent = DataAgent.from_config("config.yaml")
    result = await agent.chat("上个月收入最高的五个城市是哪些？", session_id="demo")
    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
```

返回值是 LangGraph 原生状态。通过共享 backend 生成的 SQL、CSV 和辅助文件对主 Agent 可见；消息交接与执行过程由 Deep Agents subagent middleware 管理。

## 通用 Subagents

当子 Agent 需要一份完整独立 YAML，而不是专用内联 NL2SQL 形式时，使用 `SUBAGENTS`：

```yaml
SUBAGENTS:
  - path: /absolute/path/to/researcher.yaml
  - path: /absolute/path/to/another_nl2sql.yaml
```

子 Agent 可继承父 Agent 的模型和 backend；若子配置自行声明 `MODEL`，则使用自己的模型。递归路径和重复 identifier 会在编译阶段失败。

## 运行检查

1. 在 `.env` 中检查模型供应商 API key 和可选 base URL。
2. 数据库文件使用绝对路径。
3. 确认 `DATABASE.db_id` 与 Semantic Service 中已导入的元数据一致。
4. 测试 SQL 生成前先验证 `SEMANTIC_LAYER.base_url`。
5. 主 Agent 需要继续同一个 LangGraph thread 时复用 `session_id`。

如果入口应始终直接运行 NL2SQL graph，见 [构建 NL2SQL 专用 Agent](build-an-nl2sql-application.md)。
