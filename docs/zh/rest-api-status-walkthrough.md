# REST 状态查询串讲

## 1. 背景

`POST /api/agent/operation` 在 `type=query` 之外增加两种只读操作：`semantic_status` 与 `ingestion_status`。它们不跑 NL2SQL，不改 prompt，也不改数据。原来的 `POST /api/agent/query` 不参与。提问仍走 [REST operation 串讲](rest-api-operation-walkthrough.md)。

`semantic_status` 看默认场景那份 yaml 里的语义服务进程是否响应。`ingestion_status` 按已登记场景查一条入库 trace。语义服务没有接口能证明已加载的语义在逻辑上正确。`UP` 只说明健康检查有响应。`normal` 只说明入库作业申报完成。

## 2. 方案设计

### 2.1 两个 type

默认监听与 operation 相同，`127.0.0.1:8010`。入口仍是 `POST /api/agent/operation`。`type` 取 `semantic_status` 或 `ingestion_status`。模型分别是 `SemanticStatusRequest`、`IngestionStatusRequest`。两边都是 `extra=forbid`。

`semantic_status` 的 `content` 是空对象，模型是 `SemanticStatusContent`。不接收 `scenario`。服务取默认场景的 DataAgent，用它的 `SEMANTIC_LAYER.base_url` 发 `GET /api/semantic/v1/health`。这个请求不带 `X-Semantic-Scenario`。多份 `--config scenario=path` 时，默认场景是第一个 `--config`。单个 `--config path` 时就是那一份配置。

`ingestion_status` 的 `content` 模型是 `IngestionStatusContent`：

| 字段 | 约束 |
| --- | --- |
| `type` | 字符串，取 `ingestion_status` |
| `content.scenario` | 字符串，长度至少 1。必须是启动时登记的场景名 |
| `content.traceId` | 字符串，长度至少 1 |

`content.scenario` 先选出那份 yaml 和它的 `SEMANTIC_LAYER.base_url`。未登记时 HTTP 422，体是 `{"detail": "unknown scenario: <名字>"}`，不去调语义层。语义层场景头不使用这个请求字段。只有该 yaml 写了 `SEMANTIC_LAYER.scenario` 时，才带 `X-Semantic-Scenario`，值是配置里的场景名。不写或空白则不带，语义服务进入 default schema。这个值可以和 `DATABASE.db_id`、`DATABASE.perceptor_type` 不同。

请求是 `GET /api/semantic/v1/ingestion-traces?traceId=<traceId>`。

语义层调用失败时，沿用 `_format_error`。nl2sql 场景下 `code` 是 `WORKFLOW-AGENT-001`，`component` 是 `agent`，`retryable` 是 `false`。HTTP 状态取 `result.http_status`。不把语义层的 `{error, errorCode, errorMessage}` 透传出来。

### 2.2 与场景 yaml 的关系

两份场景 yaml 可以指向不同的 `SEMANTIC_LAYER.base_url`。`semantic_status` 只看默认场景的地址。`ingestion_status` 看 `content.scenario` 选中的那份地址。

`DATABASE.db_id` 仍是逻辑库名，状态接口不用它做路由。`DATABASE.config.feature` 只给云核 SQL 试跑使用，状态接口也不读它。

## 3. 对外表现

`semantic_status`：

```bash
curl -sS -X POST "http://127.0.0.1:8010/api/agent/operation" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "semantic_status",
    "content": {}
  }'
```

语义层返回 `status` 为 `UP` 时，HTTP 200：

```json
{
  "result": {
    "success": true,
    "status": "UP"
  }
}
```

调用失败，或返回的 `status` 不是 `UP` 时，HTTP 500：

```json
{
  "result": {
    "success": false,
    "code": "WORKFLOW-AGENT-001",
    "message": "semantic service is not UP",
    "http_status": 500,
    "component": "agent",
    "retryable": false
  }
}
```

`ingestion_status`：

```bash
curl -sS -X POST "http://127.0.0.1:8010/api/agent/operation" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "ingestion_status",
    "content": {
      "scenario": "traffic_insight",
      "traceId": "T1"
    }
  }'
```

成功时 HTTP 200。`status` 保留语义层的字符串。`completed` 只在 `normal` 时为 `true`。`updating` 与 `failed` 都是 `false`，靠 `status` 区分。

```json
{
  "result": {
    "success": true,
    "status": "normal",
    "completed": true
  }
}
```

`updating`、`failed` 只替换 `status`，`completed` 为 `false`。语义层若把超过 60 分钟仍为 `updating` 的记录在响应里展示成 `failed`，本接口原样保留。

未登记场景：

```json
{
  "detail": "unknown scenario: missing"
}
```

trace 不存在时 HTTP 500，`message` 是 `ingestion trace not found`。其它语义层调用失败时 `message` 是 `failed to query ingestion status`。包的其余字段与 `semantic_status` 的失败包相同。

## 4. 规格约束与边界

- `semantic_status` 的 `content` 必须是空对象。它不发送 `X-Semantic-Scenario`，只访问默认场景的 `SEMANTIC_LAYER.base_url`。
- `ingestion_status` 的 `content.scenario` 选择 yaml。`X-Semantic-Scenario` 只来自该 yaml 的 `SEMANTIC_LAYER.scenario`。配置为空时不带头。
- `normal` 不是逻辑正确。`UP` 也不是逻辑正确。
- 未登记场景是 HTTP 422 的 `detail`，不是 `WORKFLOW-AGENT-001`。语义层调用失败才走 `WORKFLOW-AGENT-001`。
- `extra=forbid` 保持。`content` 里未声明的字段返回 422。

## 5. 测试建议

- `semantic_status` 打到第一个 `--config` 的 `base_url`，请求头里没有 `X-Semantic-Scenario`。`UP` 返回 `success: true`。非 `UP` 或调用失败返回 `semantic service is not UP`。
- `ingestion_status` 的 `scenario` 为 `traffic_insight` 时，使用那份 yaml 的 `base_url`。yaml 里写了 `SEMANTIC_LAYER.scenario` 才带头，值等于配置而不是随意换成别的字符串。
- `normal` 的 `completed` 为 `true`。`updating` 与 `failed` 的 `completed` 都为 `false`，`status` 保持原字符串。
- 未登记的 `scenario` 返回 422 `{"detail": "unknown scenario: ..."}`，且没有语义层调用。
- trace 不存在时 `message` 为 `ingestion trace not found`。
