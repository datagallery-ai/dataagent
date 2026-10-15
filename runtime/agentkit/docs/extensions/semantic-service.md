# 语义服务客户端

本文说明如何把 AgentKit 连接到已经运行的语义服务。服务本身的安装见 `services/datalink/docs/`。

客户端独立于插件注册。配置了 `semantic` 之后，REST 在进程生命周期里创建一个客户端，并放进每个 Agent 的 `ModelContext`。SDK 的 `create_agent` 在未传入客户端时自己创建，并在 `close()` 时关闭。

## 配置

```json
{
  "semantic": {
    "base_url": "https://semantic.example.com",
    "username": "$env{SEMANTIC_USERNAME}",
    "password": "$env{SEMANTIC_PASSWORD}",
    "timeout": 30,
    "tls": {
      "verify_server": true,
      "ca_file": "certs/ca.pem",
      "client_cert": "certs/client.pem",
      "client_key": "certs/client.key"
    }
  }
}
```

字段和默认值见 [配置参考](../reference/configuration.md) 的 `semantic` 一节。

- `base_url` 不能为空白。客户端会规范成服务根，请求打到 `/api/semantic/v1/...`
- `username` 和 `password` 必须同时出现或同时省略。出现时使用 HTTP Basic
- `timeout` 默认 30 秒，必须是正的有限数
- TLS 文件路径相对声明它们的配置文件目录，支持 `~/`

## TLS

`verify_server` 默认 `false`。此时若也没有客户端证书和 `ciphers`，连接保持不校验服务端证书。

`verify_server` 为 `true` 时，使用默认 SSL 上下文；提供了 `ca_file` 则用该 CA。`client_cert` 和 `client_key` 必须成对。`ciphers` 非空时传给 `set_ciphers`。

只关校验、不配证书和 cipher 时，实现返回 `verify=False`，而不是一个关了校验的上下文对象。

## 在工具里调用

```python
from langchain.tools import ToolRuntime
from dataagent import ModelContext

def lookup(query: str, runtime: ToolRuntime[ModelContext]) -> str:
    """Retrieve semantic metadata for a natural-language query."""
    client = runtime.context.semantic_client
    if client is None:
        raise RuntimeError("semantic service is not configured")
    payload = client.semantic_retrieve(query)
    return str(payload)
```

客户端是同步 `httpx.Client`。在异步工具里把调用放到线程，避免堵住事件循环。关闭时会等进行中的同步请求结束。

主要方法：

| 方法 | 路径后缀 |
| --- | --- |
| `semantic_retrieve` | `semantic/retrieve` |
| `get_table_list` | `advanced-search/table-list` |
| `get_table_columns_info` | `advanced-search/table-columns-info` |
| `get_columns_sample_values` | `advanced-search/column-value-info` |
| `semantic_search_columns` | `advanced-search/semantic-search-columns` |
| `get_joinable_tables` | `advanced-search/joinable-tables` |
| `get_table_relations_path` | `advanced-search/table-relations-path` |
| `vector_search_table_desc` | `advanced-search/vector-search-table-desc` |
| `vector_search_column_value` | `advanced-search/vector-search-column-value` |
| `sql_few_shots` | `advanced-search/sql-few-shots` |
| `search_fulltext` / `search_basic` / `search_dsl` | 对应 search 接口 |
| `get_entity_by_guid` / `get_entity_by_unique_attribute` | 实体查询 |
| `get_retrieval_table_schema` | 表结构 |

列标识使用 `db.table.column`。`get_columns_sample_values` 的 `sample_count` 范围是 0 到 100。

也可以 `SemanticServiceClient.from_settings(runtime.settings.semantic)` 自己建客户端。这样你拥有它的 `close()`。

Basic 凭据和它的 Base64 形式进入诊断脱敏列表。日志不应打出原始密钥。
