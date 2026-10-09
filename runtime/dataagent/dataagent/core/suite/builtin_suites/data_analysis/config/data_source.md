# 仓占位符

`materialize_scene.py` 将本文件复制到工作区。Skill 只使用占位符，不绑定仓品牌。
部署换仓时修改“当前取值”及元数据映射。

| 占位符 | 含义 | 当前取值 |
|---|---|---|
| `<resource_id>` | SQL MCP 资源 id | `sql_warehouse` |
| `<warehouse>` | 仓显示名，仅供方言编译 | `ClickHouse` |
| `<table_kind>` | receipt 表产物 kind | `warehouse_table`；读取时兼容 `clickhouse_table` |
| `<uri_scheme>` | receipt URI 协议 | `clickhouse` |
| `<catalog_tables>` | 表清单关系 | 见下表 |
| `<catalog_columns>` | 列清单关系 | 见下表 |
| `{{source_database}}` | 只读源库 | 任务参数 |
| `{{output_database}}` | 产物库 | 任务参数 |

## 元数据映射

| 占位符/字段 | ClickHouse 当前映射 | information_schema 仓 |
|---|---|---|
| `<catalog_tables>` | `system.tables` | `information_schema.tables` |
| `<catalog_columns>` | `system.columns` | `information_schema.columns` |
| `<database_expr>` | `database` | `table_schema` |
| `<table_name_expr>` | tables: `name`; columns: `table` | `table_name` |
| `<column_name_expr>` | `name` | `column_name` |
| `<data_type_expr>` | `type` | `data_type` |
| `<column_position_expr>` | `position` | `ordinal_position` |

URI 格式为 `<uri_scheme>://<output_database>/<table>`。当前默认可以是
ClickHouse，但 Skill、逻辑 SQL 与 workflow receipt 不得依赖该默认值。
