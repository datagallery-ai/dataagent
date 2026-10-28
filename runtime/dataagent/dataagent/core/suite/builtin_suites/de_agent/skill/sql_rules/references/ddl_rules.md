# SQL 规范（DDL）

## 适用范围

- 新建表适用下述建表规则；给已有表加列时，按 `add_columns` skill 执行，应用字段类型、命名、COMMENT 和词根校验规则。
- ALTER TABLE ADD COLUMNS 不要求附带 CREATE、PARTITIONED BY 或 STORED AS；不得改动已有表名、已有列定义和分区策略。

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
- **禁止使用当前方言不支持的类型做列声明**：Hive DDL 不得使用 `REAL`、`NUMERIC`、`FLOAT`、`DOUBLE`、`TEXT`、`VARCHAR`、`BOOLEAN`、`DATE`、`TIMESTAMP` 等白名单外类型（`REAL` 常见于 PostgreSQL/SQLite，Hive 不支持）。小数落表用 `DECIMAL(p,s)`。
- 输出：DDL 草案

## 表名与列名词根校验

- DDL 中使用的表名和列名需通过词根校验。使用 `write_file` 或 `edit_file` 写入 `create_*.sql` 后，hook 自动校验新建表的表名和列名；写入 `alter_*.sql` 后，仅校验 ADD COLUMN(S) 中的新增列，不校验已有表名，不处理修改、删除列或新增分区等其他 ALTER 操作。无需在编写 DDL 前额外调用词根校验工具。
- 对未命中词根，结合业务含义和 `candidates` 中的中文名、描述及相关度选择合适候选做替换，重新写入 DDL 后自动复验。
- 没有合适候选时，可调用 `validate_word_roots` 工具，对未命中词根提供中文描述或增大 `k` 以获取更多候选词根；仍无法确定时，向用户说明未解决的词根并请求确认，不要把未命中的词根描述为已通过校验。
- 解析失败时先修正 DDL；字典缺失、服务不可用或响应异常时，不要反复写入同一文件重试，保留候选名称继续任务，并在最终交付中说明未完成词根校验。
- 已有生产表和已有列不得自动重命名；修改用户明确指定的名称前须先与用户确认。
- 用户单独要求校验名称时，先将表名或列名按下划线拆分为词根（不含库名），再调用 `validate_word_roots`，无需生成 DDL 文件。该工具直接校验词根，不会自动拆分完整名称。
