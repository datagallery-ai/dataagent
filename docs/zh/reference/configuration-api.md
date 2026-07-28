# 配置 API 参考

这篇文档面向需要管理工作区资源的开发者。读完后，你可以创建数据源、模型、知识库、MCP Server、Skill 和文件，并把这些资源交给 Agent run 使用。

默认服务地址：

```text
http://127.0.0.1:8787
```

## 设计边界

DataFoundry 把资源管理和 Agent run 分开：

| 类型 | 入口 | 用途 |
| --- | --- | --- |
| 配置 REST API | `/api/v1/*` | 创建、测试、更新、删除资源。 |
| Agent run | `/api/copilotkit` | 启动一次数据分析运行。 |

资源先写入工作区，再通过 `run_config` 选择给本次 run 使用。

## 三层配置模型

```text
effectiveRunConfig = merge(workspaceDefaults, perRunOverrides, serverPolicy)
```

| 层级 | 来源 | 说明 |
| --- | --- | --- |
| `workspaceDefaults` | 工作区配置 | 工作区默认有哪些资源。 |
| `perRunOverrides` | 本次运行 | 用户在输入框、会话资源开关、`@` 提及中选择的资源。 |
| `serverPolicy` | 后端 | 后端权限、安全策略和能力开关。 |

后端合并后生成不可变快照，再交给 Agent Runtime。

## 鉴权

配置 API 与 AG-UI run 必须使用同一密码会话（`df_session` Cookie）。非安全方法还需要 `X-CSRF-Token`（来自 `df_csrf` Cookie）：

```text
REST /api/v1/*             -> Cookie session + X-CSRF-Token (unsafe methods)
CopilotKit /api/copilotkit -> Cookie session + X-CSRF-Token (unsafe methods)
```

这样工作区默认资源、服务端会话、文件资产、产出、SQL audit 和 run history 会留在同一个用户作用域。Web v1 不暴露 workspace 切换。

## 通用资源字段

| 字段 | 说明 |
| --- | --- |
| `id` | 稳定资源 ID。 |
| `name` | 展示名称。 |
| `description` | 资源说明。 |
| `defaultEnabled` | 是否默认给新 run 使用。 |
| `builtin` | 是否为内置资源。 |
| `revision` | 乐观并发版本。 |
| `createdAt` / `updatedAt` | 审计时间。 |

更新资源时可以传 `revision` 或 `If-Match`。冲突返回 `REVISION_CONFLICT`。

## 凭据原则

- 凭据只在创建或更新资源时提交。
- 读接口不返回明文密码、Token 或完整连接串。
- 读响应只返回 `secretRef`、`hasSecret` 或同等标记。
- 前端和 TUI 不能把凭据放进 AG-UI `messages`、`context` 或 `forwardedProps`。
- 资源支持清除凭据时，使用 `clearCredentials: true`。

## 最小创建字段

### 数据源

```json
{
  "id": "sales-pg",
  "name": "Sales PostgreSQL",
  "type": "postgresql",
  "config": {
    "host": "127.0.0.1",
    "port": 5432,
    "database": "sales",
    "username": "readonly",
    "password": "replace-with-your-key"
  }
}
```

不同数据源的字段来自 `GET /api/v1/datasource-types`。详见 [支持的数据源](supported-datasources.md)。

### 模型配置

```json
{
  "id": "qwen",
  "name": "Qwen",
  "provider": "openai-compatible",
  "model": "qwen-plus",
  "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "apiKey": "replace-with-your-key"
}
```

服务端默认模型也可以通过 `.env` 配置，不一定需要创建模型资源。

### 知识库

```json
{
  "id": "metrics-docs",
  "name": "Metrics Docs",
  "description": "指标口径文档"
}
```

创建后可以上传文件、导入文件资产、搜索和重建索引。

### MCP Server

```json
{
  "id": "local-tools",
  "name": "Local Tools",
  "transport": "streamable-http",
  "serverUrl": "http://127.0.0.1:3333/mcp"
}
```

如果使用 stdio 或带鉴权的远程服务，按后端能力开关和资源字段提交。

### Skill

Skill 使用 `multipart/form-data` 上传 package。上传后可以验证、替换和在 run 中筛选。

### 文件

`POST /api/v1/files` 使用 `multipart/form-data` 批量上传文件。返回的文件 ID 可放进 `run_config.fileIds`。

## 测试动作

| 资源 | 测试接口 |
| --- | --- |
| 数据源 | `POST /api/v1/datasources/:id/test` |
| 模型 | `POST /api/v1/model-profiles/:id/test` |
| 知识库 | `POST /api/v1/knowledge-bases/:id/test` |
| MCP Server | `POST /api/v1/mcp-servers/:id/test` |
| Skill | `POST /api/v1/skills/:id/test` |

示例：

```bash
curl -X POST http://127.0.0.1:8787/api/v1/datasources/sales-pg/test
curl -X POST http://127.0.0.1:8787/api/v1/model-profiles/qwen/test
curl -X POST http://127.0.0.1:8787/api/v1/mcp-servers/local-tools/test
```

测试响应应返回状态、延迟和诊断信息，不返回明文凭据。

## 与 Agent run 衔接

资源创建后，通过 `run_config` 选择本次运行使用哪些资源：

```json
{
  "forwardedProps": {
    "run_config": {
      "activeDatasourceId": "sales-pg",
      "enabledDatasourceIds": ["sales-pg"],
      "activeLlmProfileId": "server-default",
      "enabledKnowledgeIds": ["metrics-docs"],
      "enabledMcpServerIds": ["local-tools"],
      "skill_mode": "auto",
      "fileIds": ["file-ref-1"]
    }
  }
}
```

客户端只传资源 ID 和选择信息。后端负责校验资源是否存在、是否启用、是否允许进入本次 run。

## 并发与幂等

- `PATCH` 使用 `revision` 或 `If-Match` 防止覆盖他人更新。
- schema 抓取、知识库重建、artifact export 等异步动作可带 `Idempotency-Key`。
- 异步动作返回 job 后，用 `GET /api/v1/jobs/:id` 查询状态。

## 延伸阅读

- 端点总览：[REST API 参考](rest-api.md)
- Agent run：[Agent Runtime 与 AG-UI 参考](agent-runtime.md)
- 数据源接入：[数据源指南](../guides/data-sources.md)
