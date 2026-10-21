# SQL 规范（DDL）

## 绝对禁令（Zero-Tolerance，违反即交付失败）
以下规则具有最高优先级，必须在写任何 SQL 之前内化：
1. **DDL 必须包含 PARTITIONED BY**：每张目标表的 DDL 必须显式声明 `PARTITIONED BY (pt_* string COMMENT ...)`。没有 PARTITIONED BY 的 DDL 一律视为交付失败。*需要根据目标场景填写，见下文sql生成规则。

## SQL 方言配置（最高优先级）
本项目的 SQL 方言由 DATABASE.dialect 配置决定：
- **DDL 使用 Hive SQL 方言**：必须使用 `CREATE EXTERNAL TABLE IF NOT EXISTS`、`PARTITIONED BY`、`STORED AS ORC` 等 Hive 语法
- **禁止本地执行验证**：你没有任何本地数据库（包括 SQLite）可供执行 SQL。不要尝试执行 DDL/DML 来验证语法，不要将 SQL 改写为 SQLite 兼容语法。SQL 的正确性通过人工审查和 Gate Check 清单保证，而非本地执行。
严禁在交付 SQL 中使用 SQLite 语法（如 `INSERT OR REPLACE`、`CREATE TABLE ... PRIMARY KEY`、无 `PARTITIONED BY` 的建表语句）。

## SQL 构造
- DDL 使用 Hive：`CREATE EXTERNAL TABLE IF NOT EXISTS`、字段 `COMMENT`、`PARTITIONED BY (pt_* string COMMENT ...)`、`STORED AS ORC`。
- 普通字段类型只使用 `STRING`、`TINYINT`、`SMALLINT`、`INT`、`BIGINT`、`DECIMAL(p,s)`。
- 输出：DDL 草案
