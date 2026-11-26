# Data Gateway 使用指南

Data Gateway 是 DataAgent 的 Python 数据库访问层。脚本、服务和插件都可以用 `from dataagent import build_sql_service` 创建查询服务。调用方提供数据库连接参数和 SQL，网关负责连接、执行和取数。直接使用 SDK 无需启动 Agent、配置模型或启用插件。

## 第一次调用

在装有本仓库版本 DataAgent 的 Python 环境中运行下面的示例。它用标准库准备一个临时 SQLite 数据库，再通过网关找表并查询。接入自己的库时，直接把已有文件的路径传给 `build_sql_service` 即可。

```python
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from dataagent import build_sql_service

with TemporaryDirectory() as directory:
    path = Path(directory) / "example.sqlite"
    with closing(sqlite3.connect(path)) as setup, setup:
        setup.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount INTEGER)")
        setup.executemany("INSERT INTO orders (amount) VALUES (?)", [(25,), (40,)])

    with build_sql_service("sqlite", {"path": str(path)}, timeout_seconds=5) as db:
        print(db.search_objects("orders", exact=True)["objects"])
        columns, rows, error = db.execute_readonly(
            "SELECT SUM(amount) AS total FROM orders", limit=1
        )
        if error is not None:
            raise RuntimeError(error)
        print(columns, rows)
```

预期输出：

```text
[{'table_name': 'orders', 'object_type': 'table'}]
['total'] [(65,)]
```

## 选择引擎和连接参数

所有引擎都提供同步的 `execute(sql, limit=...)` 和 `explain(sql)`。用 `with build_sql_service(engine, config) as db:` 管理服务生命周期。第二个参数直接传下表中的字段。

| `engine` | `config` 字段 | 接入要点 |
| --- | --- | --- |
| `sqlite`、`sqlite3` | `path`，可选 `descriptions_dir` | 以只读模式打开已有文件。还提供下节的元数据查询和受限只读执行。 |
| `mysql` | `host`、`port`、`user`、`password`、`database` | 使用 PyMySQL。 |
| `gaussvector` | 同 MySQL | 使用 psycopg2。 |
| `spark`、`hive` | `warehouse_dir` | 使用启用 Hive 支持的 SparkSession，需要安装 `spark` 可选依赖。 |
| `mcp` | `path` 为 Streamable HTTP 端点；可选 `read_tool`、`timeout`、`explain_prefix` | 默认调用 `dme_read_query`，请求超时 180 秒，执行计划前缀为 `EXPLAIN PLAN FOR`。 |

对 MySQL、GaussVector、Spark 和 Hive，可先调用 `from dataagent import check_engine_driver` 导出的 `check_engine_driver(engine)`，检查本地驱动能否导入。数据库连通性要通过实际查询验证。MCP 服务端接收工具参数 `{"query": sql}`，具体返回格式见 [MCP SQL 适配器](./_mcp_sql.py)。

## 执行、取数与错误

- `explain(sql)` 请求引擎检查执行计划。成功返回 `None`，SQL 诊断通常以字符串返回。
- `execute(sql, limit=N)` 返回 `(columns, rows, error)`。成功时 `error is None`，查询失败通常返回 `(None, None, 错误文本)`。连接、驱动或 MCP 服务故障也可能抛出异常。调用方应区分查询失败和成功返回空行。
- `limit=N` 最多返回 **N+1 行**，多取的一行用于判断截断。对外展示 `rows[:N]`，用 `len(rows) > N` 标记后续还有数据。默认 `limit=-1` 取回驱动提供的全部结果。这个参数控制返回行数，不代表数据库只扫描 N 行。
- `execute()` 直接把 SQL 交给对应驱动。需要限制输入 SQL 时，调用方显式选择下文的检查接口，并给外部数据库连接配置合适的账号权限。

## SQLite 的附加能力

| 需求 | 调用 | 返回或行为 |
| --- | --- | --- |
| 找表、视图或列 | `db.search_objects("ord%", object_type="table", limit=20)` | 使用 SQL `LIKE` 模式。返回 `objects` 和 `next_offset`，后者可作为下次调用的 `offset`。找列使用 `object_type="column"`，按完整名称查找使用 `exact=True`。 |
| 了解一张表 | `db.describe_table("orders")` | 返回列定义、主键位置、外键、索引和建表语句。配置 `descriptions_dir` 时，还可从同名表的 CSV 文件合并字段说明。 |
| 执行受限只读查询 | `db.execute_readonly(sql, limit=20)` | 接收非负行数上限，保留原 SQL，使用 SQLite 的只读设置、操作授权和函数白名单，返回相同的三元组。 |
| 为一组操作设置期限 | `build_sql_service("sqlite", config, timeout_seconds=5, cancelled=event.is_set)` | 显式期限从创建服务时开始计算，同一服务内的查询共用预算。可省略取消回调。未给显式期限时，查询执行使用默认的逐条 30 秒预算。 |

工厂的 `timeout_seconds`、`cancelled` 仅适用于 SQLite。MCP 请求的超时通过连接配置里的 `timeout` 设置。

## SQL 来自用户或模型时

公共 SDK 提供两种可独立调用的检查。`check_read_only_sql(sql, dialect=...)` 检查是否为单条只读查询。`check_sql(sql, dialect=..., schema=...)` 还按策略检查语法、函数、资源使用和表结构。普通调用方可使用默认的 `gateway` 策略，原 NL2SQL 工作流使用 `policy="nl2sql"` 保留其分组、连接和语义列处理规则。

例如，报表只允许查询 `orders.amount` 时，可在上例的 `with` 块内执行以下代码：

```python
from dataagent import check_sql

sql = "SELECT SUM(amount) AS total FROM orders"
allowed_schema = {"orders": {"columns": {"amount": {"value_type": "INTEGER"}}}}
checked = check_sql(sql, dialect="sqlite", schema=allowed_schema)
if checked.blocked:
    raise ValueError([v.to_dict() for v in checked.violations])

columns, rows, error = db.execute_readonly(checked.normalized_sql or sql, limit=1)
if error is not None:
    raise RuntimeError(error)
```

如果输入是自然语言问题，需要自动挑表和探查数据，可以使用上层的 `db_explore`。已知 SQL 的调用方可以直接使用网关。仓库中可参照 [NL2SQL 执行候选 SQL](../plugins/nl2sql/nl2sql/nodes/executor.py)、[ReAct 执行受限只读查询](../plugins/nl2sql-react/nl2sql_react/tools/database.py) 和 [db_explore 读取表结构](../db_explore/database.py)。
