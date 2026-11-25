# REST 状态查询串讲

## 1. 背景

`POST /api/agent/operation` 在 `type=query` 之外增加两种只读操作：`semantic_status` 与 `ingestion_status`。它们不跑 NL2SQL，不改 prompt，也不改数据。原来的 `POST /api/agent/query` 不参与。提问仍走 [REST operation 串讲](rest-api-operation-walkthrough.md)。

两个操作的 `content.scenario` 都必填，必须是已登记场景名，不写不会落到默认场景。`semantic_status` 看该场景语义服务进程是否响应。`ingestion_status` 传 `traceId` 时查一条入库 trace，省略或传 `null` 时查该场景的第一页，最多 100 条。语义服务没有接口能证明已加载的语义在逻辑上正确。`UP` 只说明健康检查有响应。`normal` 只说明入库作业申报完成。

## 2. 方案设计

### 2.1 两个 type

默认监听与 operation 相同，`127.0.0.1:8010`。入口仍是 `POST /api/agent/operation`。`type` 取 `semantic_status` 或 `ingestion_status`。模型分别是 `SemanticStatusRequest`、`IngestionStatusRequest`。两边都是 `extra=forbid`。

`semantic_status` 的 `content.scenario` 必填，长度至少 1。它和 `ingestion_status` 一样，必须等于 `--config` 等号左边，以及该 yaml 的 `DATABASE.perceptor_type`。服务用这个名字选出 yaml，打它的 `SEMANTIC_LAYER.base_url`，发 `GET /api/semantic/v1/health`。健康检查不带 `X-Semantic-Scenario`。省略、空字符串或未登记都不走默认场景，返回 422，不调用语义层。单个 `--config path`（不带等号）没有场景名，两个状态接口都是 422。

`ingestion_status` 的 `content` 模型是 `IngestionStatusContent`：

| 字段 | 约束 |
| --- | --- |
| `type` | 字符串，取 `ingestion_status` |
| `content.scenario` | 字符串，长度至少 1。必须是启动时登记的场景名 |
| `content.traceId` | 可选。省略或传 `null` 时查询列表；传字符串时长度为 1–128，不能包含控制字符 |

`content.scenario` 先选出那份 yaml 和它的 `SEMANTIC_LAYER.base_url`。未登记时 HTTP 422，体是 `{"detail": "unknown scenario: <名字>"}`，不去调语义层。语义层场景头不使用这个请求字段。只有该 yaml 写了 `SEMANTIC_LAYER.scenario` 时，才带 `X-Semantic-Scenario`，值是配置里的场景名。不写或空白则不带，语义服务进入 default schema。这个值可以和 `DATABASE.db_id`、`DATABASE.perceptor_type` 不同。

传 `traceId` 时，请求是 `GET /api/semantic/v1/ingestion-traces?traceId=<traceId>`，返回原来的单条状态包。

省略或传 `null` 时，请求是 `GET /api/semantic/v1/ingestion-traces?limit=100&offset=0`，不发送 `traceId`。当前 operation 固定查询第一页，调用方不能指定 `limit` 或 `offset`，也不自动翻页。返回 `result.items` 列表，每项只包含 `traceId`、`status`，按语义服务返回的顺序排列。语义服务的分页字段、时间和其它字段不透传。

两种查询都要求语义层返回 JSON 对象，顶层有 `items` 数组。单条查询要求数组恰好一项，读取其 `status`。列表查询允许空数组；非空时每项都必须有非空字符串 `traceId`，以及 `normal`、`updating`、`failed` 三者之一的 `status`。缺字段或状态不合法时返回 HTTP 500 的 `result` 失败包，`message` 为 `failed to query ingestion status`，不返回部分列表。

语义层返回了错误内容时，外层仍是原来的 `result` 失败包。`code` 用下游 `errorCode`，`message` 用下游 `errorMessage`，`http_status` 和 HTTP 状态码用下游的状态码。健康检查返回 HTTP 200 但 `status` 不是 `UP` 时，同样是这个失败包，`message` 为该 `status`，`http_status` 为 200。没有 `errorCode` 时 `code` 仍是 `WORKFLOW-AGENT-001`。连不上或超时没有下游体时，`message` 用固定英文。未登记场景仍是 422。

### 2.2 与场景 yaml 的关系

两份场景 yaml 可以指向不同的 `SEMANTIC_LAYER.base_url`。`semantic_status` 和 `ingestion_status` 都看 `content.scenario` 选中的那份地址。

`DATABASE.db_id` 仍是逻辑库名，状态接口不用它做路由。`DATABASE.config.feature` 只给云核 SQL 试跑使用，状态接口也不读它。

## 3. 对外表现

`semantic_status`：

```bash
curl -sS -X POST "http://127.0.0.1:8010/api/agent/operation" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "semantic_status",
    "content": {
      "scenario": "traffic_insight"
    }
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

下游 HTTP 200 但 `status` 不是 `UP` 时，HTTP 200：

```json
{
  "result": {
    "success": false,
    "code": "WORKFLOW-AGENT-001",
    "message": "DOWN",
    "http_status": 200,
    "component": "agent",
    "retryable": false
  }
}
```

下游非 2xx 时，HTTP 状态码与下游一致，内容放进原来的失败包。例如下游 500：

```json
{
  "result": {
    "success": false,
    "code": "SEMANTIC_DOWN",
    "message": "semantic exploded",
    "http_status": 500,
    "component": "agent",
    "retryable": false
  }
}
```

`ingestion_status` 单条查询：

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

`ingestion_status` 列表查询（省略 `traceId`）：

```bash
curl -sS -X POST "http://127.0.0.1:8010/api/agent/operation" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "ingestion_status",
    "content": {
      "scenario": "traffic_insight"
    }
  }'
```

成功时 HTTP 200：

```json
{
  "result": {
    "success": true,
    "items": [
      {"traceId": "T1", "status": "normal"},
      {"traceId": "T2", "status": "updating"},
      {"traceId": "T3", "status": "failed"}
    ]
  }
}
```

列表项不附加 `completed`，调用方直接按 `status` 区分。`success: true` 仅表示状态查询成功，不表示列表中的作业均已成功完成。没有记录时返回 `{"result": {"success": true, "items": []}}`。

未登记场景：

```json
{
  "detail": "unknown scenario: missing"
}
```

trace 不存在时 HTTP 404，`code` 为 `INGESTION_TRACE_NOT_FOUND`，`message` 为下游的 `errorMessage`。其它下游非 2xx 同样放进这个失败包。没有下游 JSON 时，HTTP 500，`message` 是 `failed to query ingestion status`。

## 4. 规格约束与边界

- 两个接口的 `content.scenario` 都必填。省略或空字符串返回 422，不走默认场景。已登记的名字选出对应 yaml 的 `base_url`。
- `semantic_status` 的健康检查不发送 `X-Semantic-Scenario`。
- `ingestion_status` 的 `X-Semantic-Scenario` 只来自该 yaml 的 `SEMANTIC_LAYER.scenario`。配置为空时不带头。
- `traceId` 省略或为 `null` 时固定取 `limit=100&offset=0`，最多返回第一页 100 条；空字符串仍是 HTTP 422，不触发列表查询。
- `normal` 不是逻辑正确。`UP` 也不是逻辑正确。
- 未登记场景是 HTTP 422 的 `detail`。语义层错误仍是 `result` 失败包：`code`、`message`、`http_status` 取下游内容。没有下游 JSON 时 `code` 才是 `WORKFLOW-AGENT-001`。
- `extra=forbid` 保持。`content` 里未声明的字段返回 422。

## 5. 测试建议

- `semantic_status` 的 `scenario` 为 `traffic_insight` 时，打到那份 yaml 的 `base_url`，路径是 `/api/semantic/v1/health`，请求头里没有 `X-Semantic-Scenario`。`UP` 返回 `success: true`。下游非 2xx 时失败包的 `code` 为 `errorCode`，`message` 为 `errorMessage`，HTTP 状态码与下游一致。不写 `scenario` 是 422，且没有语义层调用。
- `ingestion_status` 的 `scenario` 为 `traffic_insight` 时，使用那份 yaml 的 `base_url`。yaml 里写了 `SEMANTIC_LAYER.scenario` 才带头，值等于配置而不是随意换成别的字符串。
- `normal` 的 `completed` 为 `true`。`updating` 与 `failed` 的 `completed` 都为 `false`，`status` 保持原字符串。
- 省略 `traceId` 或传 `null` 时，下游 query 为 `limit=100&offset=0`，不含 `traceId`。覆盖空列表、单条、多条以及 100 条，返回每条的 `traceId` 和 `status`。下游记录格式错误时不返回部分列表。
- 未登记的 `scenario` 返回 422 `{"detail": "unknown scenario: ..."}`，且没有语义层调用。
- trace 不存在时 HTTP 404，失败包 `code` 为 `INGESTION_TRACE_NOT_FOUND`，`message` 为下游 `errorMessage`。
