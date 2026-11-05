# 仓占位符

提交 SQL 和写 receipt 时，按下表的当前取值替换占位符。未另行说明时，当前仓是 ClickHouse。换仓时改「当前取值」列，元数据表改用对应的那一列。

> 机器可读镜像见 `references/data_source.json`：step2 的展开脚本（`step2_*_driver.py`）用它绑定 `<catalog_columns>` / `<catalog_tables>`（先找工作区副本，找不到则用本文件所在目录的打包副本），Agent 不需要手工替换这两个占位符。改仓时两处一起改。

## 占位符

| 占位符 | 含义 | 当前取值 |
|--------|------|----------|
| `<resource_id>` | 提交 SQL 作业的 MCP 资源 id，对应子代理 `RESOURCES[].id` | `sql_warehouse` |
| `<warehouse>` | 仓的显示名。Skill 正文不写品牌名，编译方言时用这里的名字 | `ClickHouse` |
| `<table_kind>` | receipt 里表产物的 `kind` | `warehouse_table`。读取旧回执时兼容 `clickhouse_table` |
| `<uri_scheme>` | 表 URI 的协议，与仓一致。receipt 写成 `<uri_scheme>://<output_database>/<table>` | `clickhouse` |
| `<catalog_tables>` | 列出某个库里有哪些表 | 见下方元数据表 |
| `<catalog_columns>` | 列出某张表有哪些列、类型和顺序 | 见下方元数据表 |
| `{{source_database}}` | 只读源库 | 任务参数，不在本文件里挑选 |
| `{{output_database}}` | 产物库 | 任务参数，不在本文件里挑选 |

## 元数据表

含 `<catalog_*>` 的 SQL 在提交前换成当前仓的那一列。ClickHouse 里，`system.tables` 的表名列是 `name`，`system.columns` 的表名列是 `table`。

| 占位符 | 含义 | ClickHouse | information_schema 仓 |
|--------|------|------------|------------------------|
| `<catalog_tables>` | 表清单 | `system.tables` | `information_schema.tables` |
| `<catalog_columns>` | 列清单 | `system.columns` | `information_schema.columns` |
| 库名列 | 元数据表里表示库名的列，对应 SQL 里的 `<database_expr>` | `database` | `table_schema` |
| 表名列 | 元数据表里表示表名的列，对应 `<table_name_expr>` | `system.tables` 用 `name`；`system.columns` 用 `table` | `table_name` |
| 列名列 | 元数据表里表示列名的列，对应 `<column_name_expr>` | `name` | `column_name` |
| 类型列 | 元数据表里表示列类型的列，对应 `<data_type_expr>` | `type` | `data_type` |
| 列顺序号 | 列在表中的位置，对应 `<column_position_expr>`。该关系没有这一列时，`ORDER BY` 只保留 `table_name` | `position` | `ordinal_position` |
