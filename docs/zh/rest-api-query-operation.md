# REST 操作查询（!429）

`POST /api/agent/operation`，默认地址 `http://127.0.0.1:8010`。

原来的 `POST /api/agent/query` **保留**，请求体仍是原来的 `{query, stream}`，不要改成 `type` + `content`。新结构只给 `/operation`。

兼容调用：

```bash
curl -sS -X POST "http://127.0.0.1:8010/api/agent/query" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "上个月销量最高的产品是什么？",
    "stream": false
  }'
```

`type` 是操作类型（operation）。`query` 只是操作之一；现在只实现 query，其它 type 会 422。后续可加 `update` / `view` 等，字段会不同，不要再包第三层。

## 请求体（type=query）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | 字符串 | 操作类型。取值示例：`query`（当前已实现的一种） |
| `content` | 对象 | 该操作的内容。query 时里面只有 `query`、`stream` |
| `content.query` | 字符串 | 提问文本，必填，长度至少 1 |
| `content.stream` | 布尔 | 仅 query 的 content 里有。`false` 一次返回 JSON；`true` 用 SSE |

```json
{
  "type": "query",
  "content": {
    "query": "上个月销量最高的产品是什么？",
    "stream": false
  }
}
```

## 同步

```bash
curl -sS -X POST "http://127.0.0.1:8010/api/agent/operation" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "query",
    "content": {
      "query": "上个月销量最高的产品是什么？",
      "stream": false
    }
  }'
```

## 流式

```bash
curl -sS -N -X POST "http://127.0.0.1:8010/api/agent/operation" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "type": "query",
    "content": {
      "query": "上个月销量最高的产品是什么？",
      "stream": true
    }
  }'
```

## 同步返回

外层始终是 `{"result": ...}`。

```json
{
  "result": {
    "success": true,
    "message": "SQL generated.",
    "sql": "SELECT 1"
  }
}
```

`result.success` 为 `false` 时，HTTP 状态取 `result.http_status`（缺省 500）。

## SSE

先推若干条 `event: message` 进度，最后一条是结果：

```text
event: result
data: {"result": {"success": true, "message": "SQL generated.", "sql": "SELECT 1"}}
```

`result` 的 `data` 与同步整包 JSON 相同。未知 `type` 会 422，不会当提问执行。
