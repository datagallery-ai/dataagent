---
name: dataops_probe
description: |
  Use when (任一即触发,镜像 instructions.md "何时调" 三场景):
    1. 字段是否存在/类型不明 —— 表名确定但列名来自猜测(用 `amount` 还是 `amt`?);
    2. 取值范围不清 —— 过滤条件中枚举值不确定(渠道字段到底有哪些值?);
    3. 空值分布不明 —— JOIN key 或主键是否含 NULL/空字符串未经验证。
  调用入口:工具 `dataops_probe_table`(`local_tool/dataops_probe_tool.py`)。SELECT-only 样例探索,
  默认 LIMIT 5,失败自动跳过。
  NOT for: schema 之外的"行数统计 / 数据多样性 / 业务指标计算"——那是普通 SQL 的活;
  业务上"今天"的批量数据探查——那是 DDL/DML + $date 占位符的活,DataOps 调度时替换。
---

# DataOps Probe (Sample-Row Tooling)

## When to Run

- 你刚拿到一批候选表,需要在写 DDL/DML 之前确认列名拼写、值域、是否分区表、是否空表。
- 比 schema search/get_table_desc 更进一步:返回**真实样本行**,不是元数据。
- 单次探查开销小(默认 LIMIT 5、timeout 60s),但不要在同一个 agent loop 里反复探同一张表(数据没变)。

## Core Constraints (Must Follow)

1. **`partition_filter` 只能是 `pt_d = 'YYYYMMDD'` 一种形态**。小时/分钟分区
   (`pt_h` / `pt_i`)、`dt` / `p_date` 等其它列名**一律不认** —— 哪怕表是
   `_hm` / `_im` 后缀的小时表也只用 `pt_d` 探查（探查目的是确认「列名拼写 /
   值域 / 是否有数据」，落到某个具体时刻没有意义）。
2. **绝对不要**写 `$date` / `$hour` / `$minute` 占位符。DataOps 后端**不会**替换
   这些占位符（它们是 DDL/DML 调度时的模板替换机制，probe 是临时 SELECT 探索，
   不走同一套机制），工具层也会白名单拒绝。写了会返回 `invalid_partition_filter`。
3. **不要拼日期**。工具不做日期计算；你必须把"今天"显式写成 8 位字符串。
4. **`columns` 必须显式传(不允许 None)**。DataOps 平台禁止 `SELECT *`，LLM 必须先
   通过 metadata_recall/get_table_schema 拿到表的所有列名，再选要探查的列传入。
   `columns=None` / `columns=[]` 会直接返回 `invalid_columns`，你得先查 schema 再重试。
   如果你只需要看"某几列的值"，也必须全部列出来（校验更严，白名单通过才用反引号引用）。
5. **不要盲目重试**。看到非 `completed` 结果时直接读 `reason`/`errorLog` 给用户
   解释，或换一张相邻表。

## partition_filter 规则

工具层只接受下面这**一种**整串形式（空格变体允许，但列名固定）：

| 形态 | 例 | 适用 |
|---|---|---|
| 仅天 | `pt_d = '20261012'` | **所有表**(含 `_hm` / `_im` 后缀的小时/分钟表) |

日期数字必须语义合法（`date(...)` 二次校验）：月份 1-12、日期 1-31。
**字符串合法但语义非法**（`pt_d = '20261301'`）会被工具拒掉。

**缺省时工具自动生成**（避免 LLM 漏传导致 DataOps 平台 "partition condition
not found"）：日期 = 系统当前日期 - 7 天（默认探查"前一周"，数据已稳定落盘；
今天 20261012 → 默认查 20261005），形态恒为 `pt_d = 'YYYYMMDD'`，**不按表名
后缀区分**。成功返回里会带 `auto_partition_filter` 字段告诉你实际用了什么。

需要覆盖默认值时显式传 `partition_filter`：

- 用户明确要"今天"或"前 N 天" → 自己计算 yyyymmdd 后传入。
- 业务侧已确认分区列不是 `pt_d` → 工具白名单不接受，改用普通 SQL 工具
  （`sql_reader` / `dataops_execute_sql`）。

## 典型调用

```python
# 天表
dataops_probe_table(
    table="dws.user_action_dm",
    columns=["user_id", "action_type"],
    partition_filter="pt_d = '20261012'",
    sample_size=5,
)

# 小时表 / 分钟表 —— 同样只用 pt_d,不要传 pt_h / pt_i
dataops_probe_table(
    table="dws.user_action_im",
    columns=["user_id", "action_type"],
    partition_filter="pt_d = '20261012'",
)
```

## 返回结果解读

| `ok` | `skipped` | 含义 | 你的下一步 |
|---|---|---|---|
| `True` | 缺省 | 成功拉到 `row_count` 行 | 读 `rows` 字段,做 schema linking 判断 |
| `False` | `True` | 工具主动跳过(校验失败) | 读 `reason`:`empty_table` / `invalid_table_name` / `invalid_partition_filter`,**自己改对再重试** |
| `False` | 缺省 | 后端执行失败 | 读 `errorLog`,**不要盲目重试**,换相邻表或换查询 |

## 已知工具局限(写交付物时回退到 readbook)

- **只接 `pt_d` 一个分区列名**。`pt_h` / `pt_i` / `dt` / `p_date` 等都不支持。
- 只支持字面日期,**不支持** `pt_d BETWEEN 'x' AND 'y'`、IN 列表、相对时间(`NOW() - INTERVAL 1 DAY`)。
- 不支持带函数的 WHERE、`OR` 串联、IN 子查询(都被正则拒绝)。
- 超过这些限制的探查,直接写 `SELECT ... FROM ... WHERE ...` 走标准 SQL 工具,但注意 DataOps 仍
  要求有分区列,此时你必须自己**算好日期再拼接**,不能依赖任何占位符。
