# A2A Northbound Service Interface

DataAgent's A2A northbound service is based on the Google A2A 1.0 protocol, exposing a DataAgent as a standard Agent-to-Agent service for external agents to call via JSON-RPC or REST.

---

## 1. Starting the Service

### 1.1 CLI Command

```bash
python -m dataagent serve-a2a --config <config.yaml> [options]
```

### 1.2 CLI Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--config` / `-c` | Yes | — | Path to Agent YAML config file |
| `--host` | Yes | `0.0.0.0` | Server listen address |
| `--port` / `-p` | Yes | `9999` | Server listen port |
| `--jsonrpc-path` | No | `/a2a/jsonrpc` | JSON-RPC endpoint path |
| `--rest-path` | No | `/a2a/rest` | REST endpoint path prefix |
| `--auth-token` | No | `None` | Bearer token for auth (no auth if unset) |

### 1.3 Startup Examples

```bash
# Basic startup (no auth)
python -m dataagent serve-a2a --config agent.yaml --host 0.0.0.0 --port 8999

# With Bearer token auth
python -m dataagent serve-a2a --config agent.yaml --host 0.0.0.0 --port 8999 --auth-token "my-secret-token"

# Full arguments
python -m dataagent serve-a2a \
  --config agent.yaml \
  --host 0.0.0.0 \
  --port 8999 \
  --jsonrpc-path /a2a/jsonrpc \
  --rest-path /a2a/rest \
  --auth-token "my-secret-token"
```

### 1.4 Startup Output

```
DataAgent A2A 1.0 Server
Server URL: http://0.0.0.0:9999
AgentCard:  http://0.0.0.0:9999/.well-known/agent.json
JSON-RPC:   http://0.0.0.0:9999/a2a/jsonrpc
REST:       http://0.0.0.0:9999/a2a/rest
```

---

## 2. Configuration File Requirements

The A2A service requires a YAML config file with an `AGENT_CONFIG` section. Minimal configuration:

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

The `name`, `version`, and `description` fields in `AGENT_CONFIG` map to the corresponding fields in the A2A AgentCard.

---

## 3. AgentCard Discovery

### 3.1 Endpoint

```
GET /.well-known/agent-card.json
```

This endpoint is **not protected by auth** (even when `--auth-token` is set).

### 3.2 Example Response

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

**Note**: The URLs in `supportedInterfaces` are dynamically generated based on the request's `Host` header. The scheme adjusts accordingly when the `X-Forwarded-Proto` header is present.

### 3.3 AgentCard Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Agent name, from `AGENT_CONFIG.name` |
| `description` | string | Agent description, from `AGENT_CONFIG.description` |
| `version` | string | Version, from `AGENT_CONFIG.version` |
| `capabilities.streaming` | bool | Whether streaming is supported (always `true`) |
| `defaultInputModes` | string[] | Default input modes (always `["text/plain"]`) |
| `defaultOutputModes` | string[] | Default output modes (always `["text/plain"]`) |
| `skills` | object[] | Skill list, currently registers one skill with `id="chat"` |
| `supportedInterfaces` | object[] | List of supported transport protocol interfaces |

---

## 4. JSON-RPC Interface

### 4.1 Endpoint

```
POST /a2a/jsonrpc
Content-Type: application/json
```

All JSON-RPC requests must satisfy:
- The `jsonrpc` field must be `"2.0"`
- Batch requests are not supported
- If auth is enabled, include `Authorization: Bearer <token>` header

### 4.2 Method List

| Method | Description | Streaming |
|--------|-------------|-----------|
| `SendMessage` | Send a message (non-streaming) | No |
| `GetTask` | Query task status | No |
| `ListTasks` | List tasks | No |
| `CancelTask` | Cancel a task | No |

---

### 4.3 SendMessage — Send Message (Non-streaming)

Send a message to the agent and wait for the complete result.

**Request Format:**

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
          "text": "Hello, please help me analyze the sales data"
        }
      ]
    }
  }
}
```

**params Field Reference:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | object | Yes | Message body |
| `message.role` | string | Yes | Role: `"ROLE_USER"` or `"ROLE_AGENT"` |
| `message.parts` | object[] | Yes | Array of content parts |
| `message.parts[].text` | string | No | Text content (primary support for text parts) |
| `message.parts[].data` | object | No | Structured data (JSON) |
| `message.parts[].url` | string | No | Resource URL |
| `message.task_id` | string | No | Associated task ID |
| `message.context_id` | string | No | Context ID (for multi-turn conversations) |
| `configuration` | object | No | Send configuration |
| `configuration.history_length` | int | No | Number of history messages to return |
| `configuration.return_immediately` | bool | No | Return immediately without waiting for completion |
| `metadata` | object | No | Custom metadata |

**Success Response:**

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
              "text": "Based on the analysis, the sales data shows the following trends..."
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
              "text": "Based on the analysis, the sales data shows the following trends..."
            }
          ]
        }
      ],
      "history": []
    }
  }
}
```

**Response Field Reference:**

| Field | Type | Description |
|-------|------|-------------|
| `result.task.id` | string | Task ID |
| `result.task.contextId` | string | Context ID |
| `result.task.status.state` | string | Task state (see TaskState enum below) |
| `result.task.status.message` | object | Status-associated message |
| `result.task.artifacts` | object[] | Artifact list |
| `result.task.artifacts[].name` | string | Artifact name (fixed to `"dataagent_result"` by DataAgent) |
| `result.task.artifacts[].parts[].text` | string | Artifact text content |
| `result.task.history` | object[] | Task history messages |

**TaskState Enum Values:**

| Value | Description |
|-------|-------------|
| `submitted` | Submitted |
| `working` | In progress |
| `completed` | Completed |
| `failed` | Execution failed |
| `canceled` | Canceled |
| `input-required` | Input required |
| `rejected` | Rejected |
| `auth-required` | Auth required |

**Error Response:**

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

### 4.4 GetTask — Query Task

**Request Format:**

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

**params Field Reference:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Task ID |
| `history_length` | int | No | Number of history messages to return |

**Success Response:** Returns the full Task object (same structure as the task in SendMessage).

---

### 4.5 ListTasks — List Tasks

**Request Format:**

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

**params Field Reference:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `context_id` | string | No | Filter by context ID |
| `status` | string | No | Filter by task state (TaskState enum value) |
| `page_size` | int | No | Page size (default 50, max 100) |
| `page_token` | string | No | Pagination token |
| `history_length` | int | No | History message count per task |
| `include_artifacts` | bool | No | Whether to include artifacts |

**Success Response:**

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

### 4.6 CancelTask — Cancel Task

**Request Format:**

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

**params Field Reference:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | ID of the task to cancel |

**Success Response:** Returns the canceled Task object (state: `canceled`).

---

## 5. REST Interface

### 5.1 Endpoint Overview

REST endpoints are mounted under the `/a2a/rest` path prefix.

| Method | Path | Description | Streaming |
|--------|------|-------------|-----------|
| POST | `/a2a/rest/message:send` | Send message | No |
| GET | `/a2a/rest/tasks/{id}` | Query task | No |
| GET | `/a2a/rest/tasks` | List tasks | No |
| POST | `/a2a/rest/tasks/{id}:cancel` | Cancel task | No |

### 5.2 POST /message:send — Send Message (Non-streaming)

**Request:**

```bash
curl -X POST http://127.0.0.1:9999/a2a/rest/message:send \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "role": "ROLE_USER",
      "parts": [{"text": "Hello, please help me analyze the sales data"}]
    }
  }'
```

**Request Body:** `SendMessageRequest` in JSON format (same as JSON-RPC `params`).

**Response:** `SendMessageResponse` in JSON format.

```json
{
  "task": {
    "id": "task-abc123",
    "status": {
      "state": "completed",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{"text": "Based on the analysis..."}]
      }
    },
    "artifacts": [
      {
        "name": "dataagent_result",
        "parts": [{"text": "Based on the analysis..."}]
      }
    ]
  }
}
```

### 5.3 GET /tasks/{id} — Query Task

```bash
curl http://127.0.0.1:9999/a2a/rest/tasks/task-abc123?history_length=10
```

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `history_length` | int | No | Number of history messages to return |

### 5.4 GET /tasks — List Tasks

```bash
curl "http://127.0.0.1:9999/a2a/rest/tasks?context_id=session-001&page_size=20"
```

**Query Parameters:** Same as the JSON-RPC `ListTasks` params fields.

### 5.5 POST /tasks/{id}:cancel — Cancel Task

```bash
curl -X POST http://127.0.0.1:9999/a2a/rest/tasks/task-abc123:cancel
```

---

## 6. Authentication

### 6.1 Enabling Auth

Set `--auth-token` on startup:

```bash
python -m dataagent serve-a2a --config agent.yaml --auth-token "secret123"
```

### 6.2 Auth Mechanism

- Type: Bearer Token
- All endpoints except `/.well-known/agent-card.json` require the `Authorization` header
- Invalid tokens return HTTP 401

### 6.3 Authenticated Request Example

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
        "parts": [{"text": "Hello"}]
      }
    }
  }'
```

### 6.4 Auth Error Response

```json
{
  "detail": "Unauthorized: invalid or missing Bearer token"
}
```

HTTP Status Code: `401`

---

## 7. Client Usage Examples

### 7.1 Using the a2a-sdk Python Client

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
            # Non-streaming call
            request = SendMessageRequest(
                message=new_text_message(
                    text="Please help me analyze sales data trends",
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

### 7.2 Using curl (JSON-RPC Non-streaming)

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
        "parts": [{"text": "Hello"}]
      }
    }
  }'
```

### 7.3 Using curl (REST Non-streaming)

```bash
curl -X POST http://127.0.0.1:9999/a2a/rest/message:send \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "role": "ROLE_USER",
      "parts": [{"text": "Hello"}]
    }
  }'
```

### 7.4 Multi-turn Conversation

Use `context_id` for multi-turn conversations:

```json
// First turn
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "context_id": "my-session-001",
      "parts": [{"text": "I have some sales data to analyze"}]
    }
  }
}

// Second turn (same context_id)
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "context_id": "my-session-001",
      "parts": [{"text": "Please summarize sales by region"}]
    }
  }
}
```

The `context_id` is mapped to DataAgent's internal `session_id`, ensuring context continuity within the same session.

---

## 8. Architecture

```
┌──────────────────────────────────────────────┐
│              External A2A Client              │
│  (a2a-sdk / curl / other A2A Agent)           │
└────────────┬────────────────────┬─────────────┘
             │ HTTP               │
             ▼                    ▼
┌────────────────────────┐  ┌────────────────────────┐
│  JSON-RPC Endpoint      │  │  REST Endpoint          │
│  POST /a2a/jsonrpc     │  │  /a2a/rest/*           │
│  (a2a-sdk routes)      │  │  (a2a-sdk routes)      │
└──────────┬─────────────┘  └──────────┬─────────────┘
           │                            │
           └──────────┬─────────────────┘
                      ▼
┌─────────────────────────────────────────────────┐
│            DefaultRequestHandler                 │
│            (a2a-sdk protocol layer)              │
│  - Task Store (InMemory)                        │
│  - Event Queue                                  │
│  - Agent Card                                   │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│            DataAgentExecutor                    │
│            (dataagent.a2a_server)                    │
│  - Extract user text                            │
│  - Call DataAgent.chat()                        │
│  - Publish Task/Status/Artifact events           │
│  - Support cancellation                         │
│  - Per-session FIFO serialization               │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│            DataAgent                     │
│  FlexAgent / NL2SQLAgent                        │
│  Planner → Executor loop                        │
└─────────────────────────────────────────────────┘
```

**Key Implementation Details:**

- `DataAgentExecutor` calls `DataAgent.chat()` with `session_id` (from `context_id`) and `run_id` (auto-incrementing counter)
- Per-session execution is serialized via `asyncio.Lock` for FIFO ordering
- Cancellation is implemented via `asyncio.Event`, checked after `agent.chat()` returns
- Error handling: first checks `response["error"]`, then `messages[-1].additional_kwargs["error"]`
- Result extraction priority: `final_answer` > `answer` > `result` > `response` > `messages[-1].content`

---

## 9. Notes

1. **Dependency Installation**: A2A service requires `a2a-sdk>=1.0.0`. Install with `pip install a2a-sdk[http-server]`
2. **In-Memory Task Store**: The current MVP uses `InMemoryTaskStore`; task history is lost on restart. For production, replace with PostgreSQL storage
3. **Configuration File**: Must contain the `AGENT_CONFIG` top-level field, otherwise startup fails
4. **Host Parameter**: Cannot be empty; a valid host address must be explicitly specified
5. **Dynamic AgentCard URLs**: URLs in `supportedInterfaces` are dynamically generated from the request's `Host` header, ensuring clients from different networks receive correct addresses
6. **Port Conflicts**: Default port is 9999; ensure it is not already in use
