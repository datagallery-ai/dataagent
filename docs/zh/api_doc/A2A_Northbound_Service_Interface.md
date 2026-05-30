# A2A 北向服务接口

DataAgent A2A 北向服务基于 Google A2A 1.0 协议，将 DataAgent 暴露为标准的 Agent-to-Agent 服务，供外部 Agent 通过 JSON-RPC 或 REST 协议调用。

---

## 1. 启动服务

### 1.1 CLI 命令

```bash
python -m dataagent serve-a2a --config <config.yaml> [options]
```

### 1.2 命令行参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--config` / `-c` | 是 | — | Agent YAML 配置文件路径 |
| `--host` | 是 | `0.0.0.0` | 服务监听地址 |
| `--port` / `-p` | 是 | `9999` | 服务监听端口 |
| `--jsonrpc-path` | 否 | `/a2a/jsonrpc` | JSON-RPC 端点路径 |
| `--rest-path` | 否 | `/a2a/rest` | REST 端点路径前缀 |
| `--auth-token` | 否 | `None` | Bearer Token 鉴权（不设置则无鉴权） |

### 1.3 启动示例

```bash
# 基础启动（无鉴权）
python -m dataagent serve-a2a --config agent.yaml --host 0.0.0.0 --port 8999

# 带 Bearer Token 鉴权
python -m dataagent serve-a2a --config agent.yaml --host 0.0.0.0 --port 8999 --auth-token "my-secret-token"

# 完整参数
python -m dataagent serve-a2a \
  --config agent.yaml \
  --host 0.0.0.0 \
  --port 8999 \
  --jsonrpc-path /a2a/jsonrpc \
  --rest-path /a2a/rest \
  --auth-token "my-secret-token"
```

### 1.4 启动后输出

```
DataAgent A2A 1.0 Server
Server URL: http://0.0.0.0:9999
AgentCard:  http://0.0.0.0:9999/.well-known/agent.json
JSON-RPC:   http://0.0.0.0:9999/a2a/jsonrpc
REST:       http://0.0.0.0:9999/a2a/rest
```

---

## 2. 配置文件要求

A2A 服务需要一个包含 `AGENT_CONFIG` 的 YAML 配置文件。最简配置如下：

```yaml
AGENT_CONFIG:
  name: "My Agent"
  version: "1.0.0"
  description: "My data analysis agent"
  backend: "langgraph"
  type: "react"

MODEL:
  deepseek:
    model_type: "chat"
    provider: "deepseek"
    params:
      model: "deepseek-chat"

SCENARIO:
  chat:
    instructions: "You are a helpful data analysis assistant."
```

`AGENT_CONFIG` 中的 `name`、`version`、`description` 会映射到 A2A AgentCard 的同名字段。

---

## 3. AgentCard 发现

### 3.1 端点

```
GET /.well-known/agent-card.json
```

此端点**不受鉴权保护**（即使配置了 `--auth-token`）。

### 3.2 响应示例

```json
{
  "capabilities": {
    "streaming": true
  },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "description": "DataAgent data analysis agent",
  "name": "DataAgent",
  "skills": [
    {
      "description": "Interactive conversational data analysis",
      "id": "chat",
      "inputModes": ["text/plain"],
      "name": "Chat",
      "outputModes": ["text/plain"],
      "tags": ["data-analysis", "chat"]
    }
  ],
  "supportedInterfaces": [
    {
      "protocolBinding": "JSONRPC",
      "protocolVersion": "1.0",
      "url": "http://127.0.0.1:9999/a2a/jsonrpc"
    },
    {
      "protocolBinding": "HTTP+JSON",
      "protocolVersion": "1.0",
      "url": "http://127.0.0.1:9999/a2a/rest"
    }
  ],
  "version": "0.1.0"
}
```

**注意**：`supportedInterfaces` 中的 URL 会根据请求的 `Host` 头动态生成。当使用 `X-Forwarded-Proto` 头时，scheme 会相应调整。

### 3.3 AgentCard 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | Agent 名称，来自 `AGENT_CONFIG.name` |
| `description` | string | Agent 描述，来自 `AGENT_CONFIG.description` |
| `version` | string | 版本号，来自 `AGENT_CONFIG.version` |
| `capabilities.streaming` | bool | 是否支持流式响应（固定为 `true`） |
| `defaultInputModes` | string[] | 默认输入模式（固定为 `["text/plain"]`） |
| `defaultOutputModes` | string[] | 默认输出模式（固定为 `["text/plain"]`） |
| `skills` | object[] | 技能列表，当前固定注册一个 `id="chat"` 的技能 |
| `supportedInterfaces` | object[] | 支持的传输协议接口列表 |

---

## 4. JSON-RPC 接口

### 4.1 端点

```
POST /a2a/jsonrpc
Content-Type: application/json
```

所有 JSON-RPC 请求必须满足：
- `jsonrpc` 字段必须为 `"2.0"`
- 不支持批量请求（Batch Request）
- 如需鉴权，携带 `Authorization: Bearer <token>` 头

### 4.2 方法列表

| 方法名 | 说明 | 流式 |
|--------|------|------|
| `SendMessage` | 发送消息（非流式） | 否 |
| `GetTask` | 查询任务状态 | 否 |
| `ListTasks` | 列出任务 | 否 |
| `CancelTask` | 取消任务 | 否 |

---

### 4.3 SendMessage — 发送消息（非流式）

向 Agent 发送一条消息，等待完整结果返回。

**请求格式：**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "parts": [
        {
          "text": "你好，请帮我分析一下销售数据"
        }
      ]
    }
  }
}
```

**params 字段说明：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `message` | object | 是 | 消息体 |
| `message.role` | string | 是 | 角色：`"ROLE_USER"` 或 `"ROLE_AGENT"` |
| `message.parts` | object[] | 是 | 消息内容片段数组 |
| `message.parts[].text` | string | 否 | 文本内容（当前主要支持 text part） |
| `message.parts[].data` | object | 否 | 结构化数据（JSON） |
| `message.parts[].url` | string | 否 | 资源 URL |
| `message.task_id` | string | 否 | 关联任务 ID |
| `message.context_id` | string | 否 | 上下文 ID（用于多轮会话） |
| `configuration` | object | 否 | 发送配置 |
| `configuration.history_length` | int | 否 | 返回的历史消息数 |
| `configuration.return_immediately` | bool | 否 | 是否立即返回（不等任务完成） |
| `metadata` | object | 否 | 自定义元数据 |

**成功响应：**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "task": {
      "id": "task-abc123",
      "contextId": "",
      "status": {
        "state": "completed",
        "message": {
          "role": "ROLE_AGENT",
          "parts": [
            {
              "text": "根据分析，销售数据呈现以下趋势..."
            }
          ]
        }
      },
      "artifacts": [
        {
          "artifact_id": "",
          "name": "dataagent_result",
          "description": "",
          "parts": [
            {
              "text": "根据分析，销售数据呈现以下趋势..."
            }
          ]
        }
      ],
      "history": []
    }
  }
}
```

**响应字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `result.task.id` | string | 任务 ID |
| `result.task.contextId` | string | 上下文 ID |
| `result.task.status.state` | string | 任务状态（见下方 TaskState 枚举） |
| `result.task.status.message` | object | 状态关联消息 |
| `result.task.artifacts` | object[] | 产出物列表 |
| `result.task.artifacts[].name` | string | 产出物名称（DataAgent 固定为 `"dataagent_result"`） |
| `result.task.artifacts[].parts[].text` | string | 产出物文本内容 |
| `result.task.history` | object[] | 任务历史消息 |

**TaskState 枚举值：**

| 值 | 说明 |
|------|------|
| `submitted` | 已提交 |
| `working` | 执行中 |
| `completed` | 已完成 |
| `failed` | 执行失败 |
| `canceled` | 已取消 |
| `input-required` | 需要输入 |
| `rejected` | 已拒绝 |
| `auth-required` | 需要鉴权 |

**错误响应：**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "error": {
    "code": -32603,
    "message": "Error: insufficient data for analysis"
  }
}
```

### 4.4 GetTask — 查询任务

**请求格式：**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "GetTask",
  "params": {
    "id": "task-abc123",
    "history_length": 10
  }
}
```

**params 字段说明：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 任务 ID |
| `history_length` | int | 否 | 返回的历史消息数量 |

**成功响应：** 返回完整的 Task 对象（结构同 SendMessage 中的 task）。

---

### 4.5 ListTasks — 列出任务

**请求格式：**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "ListTasks",
  "params": {
    "context_id": "session-001",
    "page_size": 20,
    "include_artifacts": false
  }
}
```

**params 字段说明：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `context_id` | string | 否 | 按上下文 ID 过滤 |
| `status` | string | 否 | 按任务状态过滤（TaskState 枚举值） |
| `page_size` | int | 否 | 每页数量（默认 50，最大 100） |
| `page_token` | string | 否 | 分页 token |
| `history_length` | int | 否 | 每个任务返回的历史消息数 |
| `include_artifacts` | bool | 否 | 是否包含产出物 |

**成功响应：**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "tasks": [
      {
        "id": "task-abc123",
        "contextId": "session-001",
        "status": {
          "state": "completed"
        }
      }
    ],
    "nextPageToken": "",
    "pageSize": 20
  }
}
```

---

### 4.6 CancelTask — 取消任务

**请求格式：**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "CancelTask",
  "params": {
    "id": "task-abc123"
  }
}
```

**params 字段说明：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 要取消的任务 ID |

**成功响应：** 返回被取消的 Task 对象（状态为 `canceled`）。

---

## 5. REST 接口

### 5.1 端点总览

REST 接口挂载在 `/a2a/rest` 路径前缀下。

| 方法 | 路径 | 说明 | 流式 |
|------|------|------|------|
| POST | `/a2a/rest/message:send` | 发送消息 | 否 |
| GET | `/a2a/rest/tasks/{id}` | 查询任务 | 否 |
| GET | `/a2a/rest/tasks` | 列出任务 | 否 |
| POST | `/a2a/rest/tasks/{id}:cancel` | 取消任务 | 否 |

### 5.2 POST /message:send — 发送消息（非流式）

**请求：**

```bash
curl -X POST http://127.0.0.1:9999/a2a/rest/message:send \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "role": "ROLE_USER",
      "parts": [{"text": "你好，请帮我分析销售数据"}]
    }
  }'
```

**请求体：** `SendMessageRequest` 的 JSON 格式（同 JSON-RPC 的 `params`）。

**响应：** `SendMessageResponse` 的 JSON 格式。

```json
{
  "task": {
    "id": "task-abc123",
    "status": {
      "state": "completed",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{"text": "根据分析..."}]
      }
    },
    "artifacts": [
      {
        "name": "dataagent_result",
        "parts": [{"text": "根据分析..."}]
      }
    ]
  }
}
```

### 5.3 GET /tasks/{id} — 查询任务

```bash
curl http://127.0.0.1:9999/a2a/rest/tasks/task-abc123?history_length=10
```

**查询参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `history_length` | int | 否 | 返回的历史消息数量 |

### 5.4 GET /tasks — 列出任务

```bash
curl "http://127.0.0.1:9999/a2a/rest/tasks?context_id=session-001&page_size=20"
```

**查询参数：** 与 JSON-RPC `ListTasks` 的 params 字段一致。

### 5.5 POST /tasks/{id}:cancel — 取消任务

```bash
curl -X POST http://127.0.0.1:9999/a2a/rest/tasks/task-abc123:cancel
```

---

## 6. 鉴权

### 6.1 启用鉴权

启动时设置 `--auth-token`：

```bash
python -m dataagent serve-a2a --config agent.yaml --auth-token "secret123"
```

### 6.2 鉴权机制

- 类型：Bearer Token
- 除 `/.well-known/agent-card.json` 外的所有接口均需携带 `Authorization` 头
- Token 不匹配时返回 HTTP 401

### 6.3 携带鉴权的请求示例

```bash
curl -X POST http://127.0.0.1:9999/a2a/jsonrpc \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer secret123" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
      "message": {
        "role": "ROLE_USER",
        "parts": [{"text": "你好"}]
      }
    }
  }'
```

### 6.4 鉴权错误响应

```json
{
  "detail": "Unauthorized: invalid or missing Bearer token"
}
```

HTTP 状态码：`401`

---

## 7. 客户端调用示例

### 7.1 使用 a2a-sdk Python 客户端

```python
import httpx
from a2a.client import create_client
from a2a.client.client import ClientConfig
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest

async def call_a2a_agent():
    base_url = "http://127.0.0.1:9999"

    async with httpx.AsyncClient(base_url=base_url) as hxc:
        client = await create_client(
            agent=base_url,
            client_config=ClientConfig(httpx_client=hxc),
        )
        async with client:
            # 非流式调用
            request = SendMessageRequest(
                message=new_text_message(
                    text="请帮我分析销售数据趋势",
                    role=Role.ROLE_USER,
                ),
            )
            async for response in client.send_message(request):
                if response.HasField("task") and response.task.artifacts:
                    for artifact in response.task.artifacts:
                        for part in artifact.parts:
                            if part.text:
                                print(part.text)
```

### 7.2 使用 curl（JSON-RPC 非流式）

```bash
curl -X POST http://127.0.0.1:9999/a2a/jsonrpc \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
      "message": {
        "role": "ROLE_USER",
        "parts": [{"text": "你好"}]
      }
    }
  }'
```

### 7.3 使用 curl（REST 非流式）

```bash
curl -X POST http://127.0.0.1:9999/a2a/rest/message:send \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "role": "ROLE_USER",
      "parts": [{"text": "你好"}]
    }
  }'
```

### 7.4 多轮对话

通过 `context_id` 实现多轮对话：

```json
// 第一轮
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "context_id": "my-session-001",
      "parts": [{"text": "我有一份销售数据需要分析"}]
    }
  }
}

// 第二轮（同一个 context_id）
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "context_id": "my-session-001",
      "parts": [{"text": "请按地区汇总销售额"}]
    }
  }
}
```

`context_id` 会被映射为 DataAgent 内部的 `session_id`，确保同一会话内的上下文连续。

---

## 8. 架构说明

```
┌──────────────────────────────────────────────┐
│              外部 A2A 客户端                     │
│  (a2a-sdk / curl / 其他 A2A Agent)            │
└────────────┬────────────────────┬─────────────┘
             │ HTTP               │
             ▼                    ▼
┌────────────────────────┐  ┌────────────────────────┐
│  JSON-RPC 端点          │  │  REST 端点              │
│  POST /a2a/jsonrpc     │  │  /a2a/rest/*           │
│  (a2a-sdk routes)      │  │  (a2a-sdk routes)      │
└──────────┬─────────────┘  └──────────┬─────────────┘
           │                            │
           └──────────┬─────────────────┘
                      ▼
┌─────────────────────────────────────────────────┐
│            DefaultRequestHandler                 │
│            (a2a-sdk 协议层)                       │
│  - Task Store (InMemory)                        │
│  - Event Queue                                  │
│  - Agent Card                                   │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│            DataAgentExecutor                    │
│            (dataagent.a2a_server)                    │
│  - 提取用户文本                                   │
│  - 调用 DataAgent.chat()                         │
│  - 发布 Task/Status/Artifact 事件                 │
│  - 支持取消                                      │
│  - 会话级 FIFO 序列化                             │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│            DataAgent                     │
│  FlexAgent / NL2SQLAgent                        │
│  Planner → Executor 循环                         │
└─────────────────────────────────────────────────┘
```

**关键实现细节：**

- `DataAgentExecutor` 通过 `DataAgent.chat()` 调用 Agent，传入 `session_id`（来自 `context_id`）和 `run_id`（自增计数器）
- 每个 session 内部通过 `asyncio.Lock` 保证 FIFO 顺序执行
- 取消通过 `asyncio.Event` 实现，在 `agent.chat()` 返回后检查
- 错误处理：优先检查 `response["error"]`，其次检查 `messages[-1].additional_kwargs["error"]`
- 结果提取：优先级为 `final_answer` > `answer` > `result` > `response` > `messages[-1].content`

---

## 9. 注意事项

1. **依赖安装**：A2A 服务需要 `a2a-sdk>=1.0.0`，使用 `pip install a2a-sdk[http-server]` 安装
2. **内存任务存储**：当前 MVP 使用 `InMemoryTaskStore`，服务重启后任务历史丢失。生产环境可替换为 PostgreSQL 存储
3. **配置文件**：必须包含 `AGENT_CONFIG` 顶层字段，否则启动失败
4. **Host 参数**：不能为空字符串，必须显式指定有效的主机地址
5. **AgentCard URL 动态生成**：服务端根据请求的 `Host` 头动态生成 `supportedInterfaces` 中的 URL，确保客户端从不同网络访问时能获取正确的地址
6. **端口冲突**：默认端口为 9999，启动前请确认端口未被占用
