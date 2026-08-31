# REST API 参考

这篇文档面向客户端开发者和集成方。读完后，你可以找到本地 API 地址、响应格式、鉴权头、资源端点、会话端点和产出端点。

默认服务地址：

```text
http://127.0.0.1:8787
```

## 通用约定

大多数 JSON 接口返回 envelope：

```json
{
  "success": true,
  "data": {}
}
```

错误响应：

```json
{
  "success": false,
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "resource not found"
  }
}
```

文件下载和 artifact 下载返回二进制响应；上传接口使用 multipart/form-data。

## 身份与鉴权

仅支持密码会话 Cookie 与 CSRF。业务请求需携带登录后的 `df_session`；非安全方法还需 `X-CSRF-Token`（来自 `df_csrf` Cookie）：

```text
X-CSRF-Token: <token_from_df_csrf_cookie>
```

Web v1 不暴露 workspace 切换；自建集成除非自行管理 workspace 路由，否则使用登录会话绑定的 workspace。`/api/v1/*` 与 `POST /api/copilotkit` 必须使用同一会话，否则 session、资源、文件、产出和 run events 会落到不同用户作用域。

## 身份接口

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/me` | 读取当前用户和 workspace。 |

## 密码认证接口


| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/auth/status` | 读取公开认证状态（含 `registrationEnabled`，不含密钥）。 |
| POST | `/api/v1/auth/register` | 创建用户账号和验证 token。 |
| POST | `/api/v1/auth/login` | 登录并设置 `df_session` 和 `df_csrf` Cookie。 |
| POST | `/api/v1/auth/verify-email` | 验证邮箱 token。 |
| POST | `/api/v1/auth/password/forgot` | 请求密码重置。 |
| POST | `/api/v1/auth/password/reset` | 使用 token 重置密码。 |
| GET | `/api/v1/auth/csrf` | 读取当前 CSRF token。 |
| POST | `/api/v1/auth/logout` | 退出当前会话。 |
| POST | `/api/v1/auth/logout-all` | 注销当前用户所有会话。 |
| GET | `/api/v1/auth/sessions` | 列出当前用户的活跃会话。 |
| DELETE | `/api/v1/auth/sessions/:id` | 注销单个会话。 |
| POST | `/api/v1/auth/password/change` | 修改当前用户密码。 |

## 健康与能力

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/healthz` | 进程存活（liveness）。 |
| GET | `/ready` | 就绪探针：Mastra / builtin 初始化完成；响应含 `startup_ms` 与 `phases`。 |
| GET | `/api/v1/capabilities` | 读取后端能力开关。 |
| GET | `/api/v1/me` | 读取当前身份。 |

```bash
curl http://127.0.0.1:8787/healthz
curl http://127.0.0.1:8787/ready
curl http://127.0.0.1:8787/api/v1/capabilities
```

## Agent Runtime

| Method | Path | 用途 |
| --- | --- | --- |
| POST | `/api/copilotkit` | 启动 Agent run，返回 AG-UI 事件流。 |
| POST | `/api/v1/runs/:id/cancel` | 取消正在运行的 run。 |

`POST /api/copilotkit` 使用 CopilotKit / AG-UI `RunAgentInput`。详见 [Agent Runtime 与 AG-UI 参考](agent-runtime.md)。

## 会话

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/sessions` | 列出服务端会话。支持 `limit`、`cursor`。 |
| PATCH | `/api/v1/sessions/:sessionId` | 更新会话标题。 |
| DELETE | `/api/v1/sessions/:sessionId` | 永久删除会话及其对话、run、产物与子分支。 |
| GET | `/api/v1/sessions/:sessionId/conversation` | 读取服务端权威对话历史。支持 `limit`。 |
| GET | `/api/v1/sessions/:sessionId/checkpoints` | 列出已持久化的上下文 checkpoint。支持 `limit`。 |
| GET | `/api/v1/sessions/:sessionId/trace-dag` | 读取 run/step/tool/output 语义图。支持 `limit`。 |
| POST | `/api/v1/sessions/:sessionId/branches` | 从已结束 run 或 checkpoint 创建持久分支。请求体：`{ "runId": "..." }` 或 `{ "checkpointId": "..." }`。 |
| GET | `/api/v1/checkpoints/:checkpointId` | 读取 checkpoint 元数据。 |
| GET | `/api/v1/checkpoints/:checkpointId/context-package` | 读取 checkpoint 元数据及上下文快照。 |

会话接口用于 Web/TUI 恢复历史、显示标题、读取 tool-call 配对，并支持从 checkpoint 重新提问。`conversation` 响应包含 `messages`、`runEventRefs`、`toolCalls`，并可包含 `checkpoints`、`branch` 和 `branches`。每个 checkpoint 从现有 run、message 和 run event 派生，包含 `runId`、`status`、消息位置范围、事件 seq 范围、开始/结束时间和可选错误信息；它表示一轮 run 的可恢复历史边界。分支会话引用父会话到 fork checkpoint 之前的历史，不复制旧消息；读取分支时返回可见父前缀加上分支自身消息。

## 工作区配置

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/workspace-config` | 读取工作区资源默认配置。 |
| PATCH | `/api/v1/workspace-config` | 更新默认启用状态。 |
| GET | `/api/v1/run-defaults` | 读取 run 默认配置。 |

## Data Link

这些路由代理当前 workspace 中已配置的兼容 Data Link 或 DataGraph MCP 资源，不提供内置图服务。

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/datalink/servers` | 列出已配置的兼容服务。 |
| GET | `/api/v1/datalink/:serverId/graph` | 读取并标准化 workspace 图。 |
| POST | `/api/v1/datalink/:serverId/explore` | 使用自然语言查询探索图。 |
| POST | `/api/v1/datalink/:serverId/tables` | 通过已配置服务添加表数据源。 |
| DELETE | `/api/v1/datalink/:serverId/tables/:tableId` | 通过已配置服务移除表。 |
| POST | `/api/v1/datalink/:serverId/rebuild` | 重建外部图。 |

`/api/v1/datagraph/*` 可作为 `/api/v1/datalink/*` 的别名。

## 数据源

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/datasource-types` | 发现支持的数据源类型和字段 schema。 |
| GET | `/api/v1/datasources` | 列出数据源。 |
| POST | `/api/v1/datasources` | 创建数据源。 |
| GET | `/api/v1/datasources/:id` | 读取数据源详情。 |
| PATCH | `/api/v1/datasources/:id` | 更新数据源。 |
| DELETE | `/api/v1/datasources/:id` | 删除数据源。 |
| POST | `/api/v1/datasources/:id/test` | 测试连接。 |
| POST | `/api/v1/datasources/:id/introspect` | 抓取 schema，返回 job。 |
| GET | `/api/v1/datasources/:id/schema` | 读取 schema 快照。支持 `q`、`includeStats`。 |
| GET | `/api/v1/datasources/:id/tables/:table/preview` | 预览表数据。支持 `schema`、`limit`、`offset`、`orderBy`。 |

后端不暴露任意 SQL REST 入口。SQL 分析通过 Agent 工具执行。

## 模型

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/model-profiles` | 列出模型配置。 |
| POST | `/api/v1/model-profiles` | 创建模型配置。 |
| GET | `/api/v1/model-profiles/:id` | 读取模型配置。 |
| PATCH | `/api/v1/model-profiles/:id` | 更新模型配置。 |
| DELETE | `/api/v1/model-profiles/:id` | 删除模型配置。 |
| POST | `/api/v1/model-profiles/:id/test` | 测试 provider。 |

## 知识库

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/knowledge-bases` | 列出知识库。 |
| POST | `/api/v1/knowledge-bases` | 创建知识库。 |
| GET | `/api/v1/knowledge-bases/:id` | 读取知识库。 |
| PATCH | `/api/v1/knowledge-bases/:id` | 更新知识库。 |
| DELETE | `/api/v1/knowledge-bases/:id` | 删除知识库。 |
| POST | `/api/v1/knowledge-bases/:id/test` | 验证配置。 |
| GET | `/api/v1/knowledge-bases/:id/files` | 列出文档。 |
| POST | `/api/v1/knowledge-bases/:id/files` | 上传文档。 |
| DELETE | `/api/v1/knowledge-bases/:id/files/:documentId` | 硬删除单个文档（级联清理 chunks/FTS/embeddings）。 |
| POST | `/api/v1/knowledge-bases/:id/files/:documentId/reindex` | 重试/重建单个文档向量；成功后 status 置为 `ready`。 |
| POST | `/api/v1/knowledge-bases/:id/files/import` | 从 FileAssetRef 导入文档。 |
| POST | `/api/v1/knowledge-bases/:id/search` | 检索调试。 |
| POST | `/api/v1/knowledge-bases/:id/reindex` | 重建索引，返回 job；成功后文档 status 置为 `ready`。 |

## MCP 与 Skill

| Method | Path | 用途 |
| --- | --- | --- |
| GET / POST | `/api/v1/mcp-servers` | 列出或创建 MCP Server。 |
| GET / PATCH / DELETE | `/api/v1/mcp-servers/:id` | 读取、更新或删除 MCP Server。 |
| POST | `/api/v1/mcp-servers/:id/test` | 测试 MCP Server。 |
| GET | `/api/v1/mcp-servers/:id/tools` | 拉取 tools manifest。 |
| GET / POST | `/api/v1/skills` | 列出或上传 Skill。 |
| POST | `/api/v1/skills/select` | 预览本次 run 的 Skill 筛选结果。 |
| GET / PATCH / DELETE | `/api/v1/skills/:id` | 读取、更新或删除 Skill。 |
| POST | `/api/v1/skills/:id/test` | 测试 Skill。 |
| POST | `/api/v1/skills/:id/validate` | 验证 Skill。 |
| POST | `/api/v1/skills/:id/replace` | 替换 Skill package。 |

## 文件

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/files` | 列出文件资产。支持 `scope`、`origin`、`source`、`sessionId`。 |
| POST | `/api/v1/files` | 批量上传文件。需在 multipart 字段或请求头提供 session id。 |
| GET | `/api/v1/files/:id` | 读取文件引用。 |
| POST | `/api/v1/files/:id/promote` | 把 session-scoped 文件提升为跨会话工作区文件。 |
| DELETE | `/api/v1/files/:id` | 删除文件引用。 |
| GET | `/api/v1/files/:id/download` | 下载文件内容。 |
| POST | `/api/v1/chat/uploads` | 上传本次对话附件。 |

## Artifact

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/artifacts?sessionId=:sessionId` | 列出某个会话的产出。 |
| GET | `/api/v1/artifacts/:id` | 读取产出详情。 |
| GET | `/api/v1/artifacts/:id/preview` | 读取预览 JSON。 |
| GET | `/api/v1/artifacts/:id/content` | 读取 inline 内容。 |
| GET | `/api/v1/artifacts/:id/download` | 下载产出。可带 `format`。 |
| POST | `/api/v1/artifacts/:id/promote` | 把文件型产出加入工作区文件。 |
| POST | `/api/v1/artifacts/:id/export` | 导出指定格式，返回 job。 |

## Query History

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/query-history` | 列出 SQL 查询历史。支持 `sessionId`、`datasourceId`、`favorite`、`limit`。 |
| POST | `/api/v1/query-history/:id/favorite` | 收藏一条查询。 |
| POST | `/api/v1/query-history/:id/unfavorite` | 取消收藏。 |
| PATCH | `/api/v1/query-history/:id` | 用 `{ "favorite": true \| false }` 更新收藏状态。 |

## Jobs

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/jobs/:id` | 查询异步任务。 |
| POST | `/api/v1/jobs/:id/cancel` | 取消异步任务。 |

## 写入约定

- JSON 请求体默认上限为 1 MiB。
- `PATCH` 支持 `revision` 或 `If-Match` 乐观并发控制。
- schema 抓取、索引重建、artifact export 可使用 `Idempotency-Key`。
- 凭据只在创建或更新资源时提交。
- 读接口不返回明文密码、Token 或完整连接串。

## 延伸阅读

- 配置模型和资源边界：[配置 API 参考](configuration-api.md)
- 数据源接入：[数据源指南](../guides/data-sources.md)
- Agent run：[Agent Runtime 与 AG-UI 参考](agent-runtime.md)
