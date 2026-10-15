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

1. **`partition_filter` 可省略,工具自动按"系统前一周 + 表后缀"生成**。DataOps SQL 校验器要求
   `WHERE/ON` 出现分区列(readbook 第 13-15 节 + `verify.py` L107-108),工具默认会
   按表名后缀规则自动补一份默认值(详见 §"默认 partition_filter");**但 LLM 仍
   应优先通过 metadata_recall/get_table_schema 确认分区列名**,仅当工具默认拼出的
   形态不符合业务时(例如要查"今天"、跨天、近 7 天),才**显式传** `partition_filter`
   覆盖。成功返回里会带 `auto_partition_filter` 字段,告诉你工具实际用了什么。
2. **`partition_filter` 必须是已落地的字面日期**,格式严格按工具白名单,详见 §"partition_filter 格式"。
3. **绝对不要**在 `partition_filter` 里写 `$date` / `$hour` / `$minute` 占位符。DataOps 后端**不会**替换
   这些占位符(它们是 DDL/DML 调度时的模板替换机制,probe 是临时 SELECT 探索,不走同一套机制),
   工具层也会白名单拒绝。如果写了,工具返回 `invalid_partition_filter`,你得自己改对。
4. **不要拼日期**。工具不做日期计算;你(SKILL 层)必须把"今天"显式写成 8 位字符串。
5. **`columns` 必须显式传(不允许 None)**。DataOps 平台禁止 `SELECT *`,LLM 必须先
   通过 metadata_recall/get_table_schema 拿到表的所有列名,再选要探查的列传入。
   `columns=None` / `columns=[]` 会直接返回 `invalid_columns`,你得先查 schema 再重试。
   如果你只需要看"某几列的值",也必须全部列出来(校验更严,白名单通过才用反引号引用)。
6. **超时与重试**:probe 内部已经做了 timeout 夹紧(<= 300s)和 status 映射。**不要**在
   agent loop 里看到 `status=running` 之外的非 `completed` 结果时盲目重试,直接读 `reason`/`errorLog` 给
   用户解释,或换一张相邻表。

## partition_filter 格式

工具层只接受下面 3 种**整串**形式(任何空格变体都允许,但 token 顺序和列名固定):

| 形态 | 例 | 适用 |
|---|---|---|
| 仅天 | `pt_d = '20261012'` | 后缀为 `_dm` / `_ds` / 无后缀的天表 |
| 天+小时 | `pt_d = '20261012' AND pt_h = '2026101214'` | 后缀为 `_hm` 的小时表 |
| 天+小时+分钟 | `pt_d = '20261012' AND pt_h = '2026101214' AND pt_i = '202610121430'` | 后缀为 `_im` 的分钟表 |

日期数字必须语义合法(`date(...)` 二次校验):月份 1-12、日期 1-31、小时 0-23、分钟 0-59。
**字符串合法但语义非法**(`pt_d = '20261301'`)会被工具拒掉。

## 默认 partition_filter

`partition_filter` 缺省时,工具按下列规则自动生成(避免 LLM 漏传导致 DataOps 平台
"partition condition not found"):

1. **日期** = 系统当前日期 - 7 天(默认探查"前一周",数据已稳定落盘)。
   - 例:今天 20261012 → 工具默认查 20261006。
   - 跨月/跨年自动处理(`timedelta(days=7)`)。
2. **形态** = 按表名最后一个 `_xxx` 段:
   - `_im` → `pt_d = 'YYYYMMDD' AND pt_h = 'YYYYMMDD00' AND pt_i = 'YYYYMMDD0000'`(00 时/0000 分是兜底)
   - `_hm` → `pt_d = 'YYYYMMDD' AND pt_h = 'YYYYMMDD00'`
   - 其他(`_dm` / `_ds` / 无后缀) → `pt_d = 'YYYYMMDD'`
3. **覆盖场景**:工具默认拼出来不符合业务时,LLM 显式传 `partition_filter` 覆盖。常见场景:
   - 用户明确要"今天"或"前 N 天" → 自己计算 yyyymmdd 后传入。
   - 业务侧已确认分区列不是 `pt_d`(如 `dt` / `p_date`) → 工具白名单不接受,
     改用普通 SQL 工具(`sql_reader` / `dataops_execute_sql`)。
4. **不熟悉的表** —— 先 `get_table_desc` 看是不是 `pt_d` 分区,再判断要不要覆盖默认。
   如果列名是 `dt` / `p_date` / `partition_key` 等**非白名单**列名,工具**没法**接受 —— 这是已知的
   工具局限,这种情况下用普通 SQL 工具(`sql_reader` / `dataops_execute_sql`)做更灵活探查。

## 典型调用

```python
# 天表
dataops_probe_table(
    table="dws.user_action_dm",
    columns=["user_id", "action_type"],
    partition_filter="pt_d = '20261012'",
    sample_size=5,
)

# 分钟表
dataops_probe_table(
    table="dws.user_action_im",
    partition_filter="pt_d = '20261012' AND pt_h = '2026101214' AND pt_i = '202610121430'",
)
```

## 返回结果解读

| `ok` | `skipped` | 含义 | 你的下一步 |
|---|---|---|---|
| `True` | 缺省 | 成功拉到 `row_count` 行 | 读 `rows` 字段,做 schema linking 判断 |
| `False` | `True` | 工具主动跳过(校验失败) | 读 `reason`:`empty_table` / `invalid_table_name` / `invalid_partition_filter`,**自己改对再重试** |
| `False` | 缺省 | 后端执行失败 | 读 `errorLog`,**不要盲目重试**,换相邻表或换查询 |

## 已知工具局限(写交付物时回退到 readbook)

- 只接 `pt_d` / `pt_h` / `pt_i` 列名,其他分区列名不支持。
- 只支持字面日期,**不支持** `pt_d BETWEEN 'x' AND 'y'`、IN 列表、相对时间(`NOW() - INTERVAL 1 DAY`)。
- 不支持带函数的 WHERE、`OR` 串联、IN 子查询(都被正则拒绝)。
- 超过这些限制的探查,直接写 `SELECT ... FROM ... WHERE ...` 走标准 SQL 工具,但注意 DataOps 仍
  要求有分区列,此时你必须自己**算好日期再拼接**,不能依赖任何占位符。
