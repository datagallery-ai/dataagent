# REST operation 串讲

## 1. 背景

`POST /api/agent/query` 的请求体是 `query` 和 `stream`。`query` 必填，长度至少 1。`stream` 默认 `false`。没有 `type`，没有 `content`，也没有 `scenario`。

在这个入口之外使用 `POST /api/agent/operation`。同一个入口用 `type` 区分操作。`type` 选出操作，`content` 放这次操作的内容。查询操作是 `type=query`。`content` 里有 `query`、`stream`，以及可选的 `scenario`。两条查询最后都进 `_dispatch_query`，问句和流式开关相同，结果包相同。

多场景挂在 operation 上。一次启动加载多份场景 yaml。进程里按场景名持有多个 DataAgent。调用方要选场景时，只在 `type=query` 的 `content.scenario` 里带场景名。`POST /api/agent/query` 不能选场景，始终打到默认场景。多份 `--config scenario=path` 时，默认场景是命令行里的第一个。单个 `--config path`（路径里不带等号）仍是原来的启动方式，这一份配置就是默认场景。没有 `--default-scenario`。

`semantic_status` 与 `ingestion_status` 见 [REST 状态查询串讲](rest-api-status-walkthrough.md)。

## 2. 方案设计

### 2.1 operation 接口

默认监听 `127.0.0.1:8010`。`--host` 默认 `127.0.0.1`，`--port` 默认 `8010`，`--workers` 默认 `1`。配置路径写入环境变量 `DATAAGENT_REST_CONFIG`。多场景路由表写入 `DATAAGENT_REST_ROUTES`。

`GET /health` 看服务是否已经 `initialize()`。就绪返回 `{"status": "ok"}`。还没就绪返回 HTTP 503，体是 `{"status": "not_ready"}`。

`POST /api/agent/query` 的模型是 `DataAgentQueryRequest`：

| 字段 | 约束 |
| --- | --- |
| `query` | 字符串，长度至少 1 |
| `stream` | 布尔，默认 `false` |

`extra=forbid`。多出来的字段会 422。旧请求体里不能加 `scenario`、`type` 或 `content`。

`POST /api/agent/operation` 且 `type=query` 时，模型是 `QueryRequest`。`content` 的模型是 `QueryContent`。`query` 与 `stream` 放在 `content` 里。`scenario` 放在同一层 `content` 上，可以省略。

| 字段 | 约束 |
| --- | --- |
| `type` | 字符串，取 `query` |
| `content.query` | 字符串，长度至少 1 |
| `content.stream` | 布尔，默认 `false`。只放在 query 的 `content` 里 |
| `content.scenario` | 字符串，可省略。省略时与旧 query 一样走默认场景 |

`QueryRequest` 与 `QueryContent` 都是 `extra=forbid`。未声明的字段返回 422。

`stream` 为 `false` 时走 `service.query`，一次返回 JSON。`stream` 为 `true` 时返回 `text/event-stream`。事件由 `sse_event` 打成 `event:` 加 `data:`。

`message` 事件的 `data` 是 `{"message": "..."}`。最后一条 `result` 事件的 `data` 与同步整包相同，外层是 `result`。

同步成功时，NL2SQL（两份场景 yaml 的 `AGENT_CONFIG.type` 都是 `nl2sql`）由 `_format_nl2sql_result` 组装。外层是 `result`。里面的字段是：

| 字段 | 来源 |
| --- | --- |
| `success` | `true` |
| `message` | 有预览行时是 `SQL generated and executed with preview rows.`；没有 SQL 时是 `No executable SQL was generated.`；其余是 `SQL generated.` |
| `candidates` | 每项有 `index`、`sql`、`sql_sha256`。`sql_sha256` 是把 SQL 空白压成单空格再去掉首尾空白后的 SHA-256 十六进制 |
| `sql` | 最终 SQL，可以是空字符串 |
| `sql_fingerprint` | 仅当 `sql` 非空时带上，算法与 `sql_sha256` 相同 |
| `confidence` | 状态里的 `confidence` |
| `columns` | 状态里的 `columns` |
| `rows_preview` | 状态里的 `rows_preview` |
| `session_id` | 状态里的 `session_id` |

`candidates` 按 `execution_results`、`validation_results`、`generation_results` 的顺序取第一份非空列表。列表空但 `sql` 非空时，补一条 `index` 为 0 的候选。

执行失败时，`_format_error` 的包是：

```json
{
  "result": {
    "success": false,
    "code": "WORKFLOW-AGENT-001",
    "message": "internal error",
    "http_status": 500,
    "component": "agent",
    "retryable": false
  }
}
```

`AGENT_CONFIG.type` 是 `nl2sql` 时，缺省 `code` 是 `WORKFLOW-AGENT-001`。其它类型缺省是 `REACT-AGENT-001`。HTTP 状态取 `result.http_status`，缺这个字段时按 500。同步路径把整个 `{"result": ...}` 原样返回。

上游错误如果自带 `code`、`http_status`、`component`、`retryable`，并且类型对得上，会写进这份包。`component` 为 `sql_security` 时，还可以带 `errors`。每一项只保留 `code` 和 `message`，`code` 必须以 `NL2SQL-SEC-` 开头。

未登记的 `type` 在进 agent 之前就被拒绝。`type` 为 `other` 时 HTTP 422，体是 Pydantic 的 `union_tag_invalid`。当前已登记的 `type` 是 `query`、`view`、`update`、`semantic_status`、`ingestion_status`。

未登记的 `content.scenario` 也在进查询之前拒绝。HTTP 422，体是 `{"detail": "unknown scenario: <名字>"}`。旧的 `POST /api/agent/query` 不接收场景名，不会因为没写场景名返回这个 422。

### 2.2 场景路由

场景路由是一个进程重复加载多份 yaml。进程内按场景名持有多个 DataAgent。调用方只在 `POST /api/agent/operation` 且 `type=query` 时用 `content.scenario` 选择。`POST /api/agent/query` 仍是 `query` 和 `stream`，固定用默认场景。

一个进程启动。`--config` 重复传递，每一项写成 `场景名=yaml路径`。等号左边是请求里的 `content.scenario`，右边是该场景的 yaml 文件。场景名由这项参数给定，不是从文件名推出来的。第一个 `--config` 是默认场景，供旧 query 以及省略 `content.scenario` 的 `type=query` 使用。

也可以只传一个不带等号的 `--config path`。这时没有场景表，进程只加载这一份 yaml。旧 query 跑的就是它。这时 operation 上任何 `content.scenario` 都是未知场景。

启动入口是 `uv run -m dataagent`。参数由 `dataagent.interface.rest_api.start_service` 解析。`--host` 默认 `127.0.0.1`，`--port` 默认 `8010`。

```bash
uv run -m dataagent \
  --config business_twin=dataagent/agents/nl2sql/business_twin.yaml \
  --config traffic_insight=dataagent/agents/nl2sql/traffic_insight.yaml \
  --host 127.0.0.1 \
  --port 8010
```

上面这条命令里，默认场景是 `business_twin`。`POST /api/agent/query` 打到 `dataagent/agents/nl2sql/business_twin.yaml`。`content.scenario` 为 `traffic_insight` 时，走 `dataagent/agents/nl2sql/traffic_insight.yaml`。

同一个场景名出现两次，或两份 `--config` 指向同一份 yaml，启动失败，后一次不覆盖前一次。场景名必须等于该 yaml 的 `DATABASE.perceptor_type`，对不上也启动失败。

同进程可以。两份场景 yaml 的证书是同一个，进程级共用没有问题。模型用不同名字，避开 `LLMManager` 按缓存名覆盖。

证书按文件写。`dataagent/agents/nl2sql/business_twin.yaml` 和 `dataagent/agents/nl2sql/traffic_insight.yaml` 里生效的 `certificate` 都是：

```yaml
certificate:
  inbound_encrypted: true
  outbound_encrypted: true
```

两份按同一套材料共用。`DataAgent.from_config` 把 `certificate` 交给 `apply_certificate_config`，写成进程环境变量 `DATAAGENT_OUTBOUND_CA_FILE`、`DATAAGENT_OUTBOUND_CLIENT_CERT`、`DATAAGENT_OUTBOUND_CLIENT_KEY`、`DATAAGENT_OUTBOUND_CIPHERS`、`DATAAGENT_OUTBOUND_MODE`、`DATAAGENT_OUTBOUND_SSL_SERVICES`。缺省也相同：`outbound_enabled` 开启，`outbound_ssl_services` 是 `llm,semantic_layer,cloud_core`，`outbound_certificate_mode` 是 3。后加载再写一遍这些变量，写进去的仍是这一套。

模型按文件写。两份 yaml 的 `MODEL` 段名都是 `chat_model`，都没有单独的 `name`。`model_type` 都是 `chat`，`provider` 都是 `bailian`，`params.model` 都是 `qwen3-30b-a3b`，`temperature` 都是 `0.0`。

`LLMManager` 是进程单例。`init_from_config` 遍历 `MODEL`。没有 `name` 时，缓存名用段名。所以两份的缓存名都是 `chat_model`。`create_llm` 发现同名已经在 `llm_cache` 里，会打警告并覆盖。`params.model` 是供应商模型标识，覆盖不看这个字符串。

场景路由要求各场景使用不同的模型缓存名。段名不同，或显式写不同的 `name`，缓存里就各留一份。每个场景按自己的缓存名取模型。

按场景写在各自 yaml 里、并由该 agent 的配置读取的部分，按场景隔离。`from_config` 为每个 agent 单独建 `ConfigManager`。`NL2SQLAgent._perceptor_class` 用 `DATABASE.perceptor_type` 选感知节点。

| 配置 | business_twin.yaml | traffic_insight.yaml |
| --- | --- | --- |
| `CORE.perceptor.user_sql_rules` | `sql_rules_business_twin` | `sql_rules_traffic_insight` |
| `DATABASE.perceptor_type` | `business_twin` | `traffic_insight` |
| `DATABASE.db_id` | `business_twin` | `yunhe` |
| `DATABASE.config.feature` | `business_twin` | `traffic_insight` |
| `DATABASE.dialect` | `postgres` | `postgres` |
| `DATABASE.engine` | `cloud_core` | `cloud_core` |

`business_twin` 的感知节点类是 `BusinessTwinPerceptorNode`。`traffic_insight` 的是 `TrafficInsightPerceptorNode`。SQL 规则在感知时经 `_load_prompt` 读取，名字来自该节点的 `user_sql_rules`。语义层也在各自 yaml 里：`SEMANTIC_LAYER.business_twin.table_selection.mode` 是 `business_family`；`SEMANTIC_LAYER.traffic_insight.recall` 带 `column_eq_page_size`、`column_eq_max_offset`、`hybrid_batch_size`、`candidate_table_families_limit`、`exclude_table_prefix`。这些都跟该 DataAgent 的配置走。

`DATABASE.db_id` 是语义元数据里的逻辑库名，用来在已经进入的 schema 里过滤表和列。`DATABASE.config.feature` 只在云核 `explain_url` 试跑 SQL 时放进请求体，不参与选场景，也不参与执行 SQL。

## 3. 对外表现

默认地址 `http://127.0.0.1:8010`。入站证书配齐后，同一 host 和 port 走 https。

按场景名登记两份 yaml：

```bash
uv run -m dataagent \
  --config business_twin=dataagent/agents/nl2sql/business_twin.yaml \
  --config traffic_insight=dataagent/agents/nl2sql/traffic_insight.yaml \
  --host 127.0.0.1 \
  --port 8010
```

旧 query 不带场景，打到第一个 `--config`，这里是 `business_twin`：

```bash
curl -sS -X POST "http://127.0.0.1:8010/api/agent/query" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "上个月销量最高的产品是什么？",
    "stream": false
  }'
```

`POST /api/agent/operation` 的 `type=query` 在 `content` 里带 `query`、`stream` 和 `scenario`。`scenario` 取上面等号左边的名字：

```bash
curl -sS -X POST "http://127.0.0.1:8010/api/agent/operation" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "query",
    "content": {
      "query": "上个月销量最高的产品是什么？",
      "stream": false,
      "scenario": "traffic_insight"
    }
  }'
```

`content.scenario` 为 `traffic_insight` 时，走 `--config traffic_insight=dataagent/agents/nl2sql/traffic_insight.yaml`。不传 `content.scenario` 时与旧 query 相同，走 `business_twin`。响应里不回写 `scenario`。未登记的名字返回 HTTP 422，体是 `{"detail": "unknown scenario: <名字>"}`。

成功时 HTTP 200。字段与 `_format_nl2sql_result` 一致。旧 query 与 `type=query` 的结果包相同。下面的 SQL 和空值是示例，键是结果包里的键：

```json
{
  "result": {
    "success": true,
    "message": "SQL generated.",
    "candidates": [
      {
        "index": 0,
        "sql": "SELECT 1",
        "sql_sha256": "e004ebd5b5532a4b85984a62f8ad48a81aa3460c1ca07701f386135d72cdecf5"
      }
    ],
    "sql": "SELECT 1",
    "sql_fingerprint": "e004ebd5b5532a4b85984a62f8ad48a81aa3460c1ca07701f386135d72cdecf5",
    "confidence": null,
    "columns": null,
    "rows_preview": null,
    "session_id": null
  }
}
```

有预览行时 `message` 改为 `SQL generated and executed with preview rows.`，并带上状态里的 `columns`、`rows_preview`。

失败时 HTTP 状态取 `result.http_status`。未捕获异常的包：

```json
{
  "result": {
    "success": false,
    "code": "WORKFLOW-AGENT-001",
    "message": "internal error",
    "http_status": 500,
    "component": "agent",
    "retryable": false
  }
}
```

流式把 `stream` 设为 `true`。先有若干条 `event: message`，`data` 是 `{"message": "<进度文本>"}`。进度文本来自流里的 `stream_message` 或 custom 消息。最后一条是 `event: result`，`data` 与同步整包相同。`sql` 为空时没有 `sql_fingerprint`，`message` 为 `No executable SQL was generated.`。

## 4. 规格约束与边界

- 旧 query 的请求体只有 `query` 和 `stream`。它不能在多个场景里选择，固定使用第一个 `--config`；单个不带等号的 `--config` 时使用那一份 yaml。
- `type=query` 的 `content.scenario` 必须和启动参数 `--config` 等号左边的场景名一致。没有这项名字时 HTTP 422，体是 `{"detail": "unknown scenario: <名字>"}`。同一个场景名或同一份 yaml 在启动参数里出现两次，启动失败，后一次不覆盖前一次。场景名必须等于该 yaml 的 `DATABASE.perceptor_type`。
- 各场景的模型缓存名必须不同。缓存名是 `MODEL` 段名或该段的 `name`。同名会互相覆盖。取模型时按各自的缓存名取。
- 出站证书按同一套材料共用。入站 TLS、监听端口、限流是整个进程一套。
- `extra=forbid` 保持。`content` 里未声明的字段返回 422。`POST /api/agent/query` 不带 `scenario`。

## 5. 测试建议

- 同样的 `query`、`stream`，旧 `/api/agent/query` 与省略 `content.scenario` 的 `type=query`，都打到第一个 `--config`，结果包一致。
- `scenario` 为 `business_twin`、`traffic_insight` 时分别走到启动参数里同名的那份 yaml，规则和库不串。
- 请求里的 `scenario` 在启动参数里没有同名项时，HTTP 422，体是 `{"detail": "unknown scenario: ..."}`。
- 旧 query 请求体多写 `scenario` 或 `type` 时，422 的类型是 `extra_forbidden`。
- 启动参数里同一个场景名或同一份 yaml 出现两次时，进程起不来。`DATABASE.perceptor_type` 与场景名不一致时，进程起不来。
- 两个场景缓存名不同时互不覆盖；缓存名相同会覆盖，作为反例。
