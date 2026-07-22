# step1_3: build_training_set

<入口规则>两种模式均执行本步，按 plan 的 `mode` 走对应分支</入口规则>

**目的**：产出一张中间表 **`step1_temp_sampled_users(user_key, label)`**，供 step1_4 裁剪所有源表。

**硬约束：正负比 1:4**。正样本取 `min(正样本池总量, sample_size / 5)`，负样本 = 正样本 × 4；最终总量 ≤ sample_size。

## 前置

`read` `step1_0_table_schema.json` 与 `step1_0_sampling_plan.json`。列类型取自 schema。

需要判定的 plan 字段：`mode`（决定走下面哪条分支）、`sampling_sources`、`keys`、`sql_fragments`、`y_label`、`negative_populations`、`sample_size`。

---

## 分支判定

| `plan.mode` | 执行 |
|---|---|
| `"prelabeled"` | 走下面 prelabeled 分支（用户表已有 label，直接从 label 列抽样） |
| 其它（`regular` / `cold_start`） | 走下面主分支（事件口径：候选池 ⊗ label → 正负样本 → 下采样） |

---

### prelabeled 分支

<必须>正样本上限 `sample_size / 5`，负样本 = 正 × 4，总量 ≤ `sample_size`。只替换占位符，不改语句形状。</必须>

先预检（`LIMIT 1` 确认语句可执行），再建正式表。<必须>两条 LIMIT 不可改：正样本 = `CAST(<sample_size> / 5 AS UInt64)`，负样本 = `(SELECT count() * 4 FROM pos_limited)`，禁止直接写成 `<sample_size>` 字面量。</必须>

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_temp_sampled_users
ENGINE = MergeTree()
ORDER BY tuple()
AS
WITH
  pos AS (
    SELECT DISTINCT <sql_fragments.user_key_expr> AS user_key
    FROM {{source_database}}.<sampling_sources.user_table>
    WHERE <sql_fragments.valid_user>
      AND <keys.label_column> = <label_pos_val>
  ),
  pos_limited AS (
    SELECT user_key
    FROM pos
    ORDER BY cityHash64(user_key)
    LIMIT CAST(<sample_size> / 5 AS UInt64)
  ),
  neg_pool AS (
    SELECT DISTINCT <sql_fragments.user_key_expr> AS user_key
    FROM {{source_database}}.<sampling_sources.user_table>
    WHERE <sql_fragments.valid_user>
      AND <keys.label_column> = <label_neg_val>
  ),
  neg_sampled AS (
    SELECT user_key
    FROM neg_pool
    ORDER BY cityHash64(user_key)
    LIMIT (SELECT count() * 4 FROM pos_limited)
  ),
  combined AS (
    SELECT user_key, toUInt8(1) AS label FROM pos_limited
    UNION ALL
    SELECT user_key, toUInt8(0) AS label FROM neg_sampled
  ),
  deduped AS (
    SELECT user_key, max(label) AS label
    FROM combined
    GROUP BY user_key
  )
SELECT user_key, label
FROM deduped
ORDER BY cityHash64(user_key);
```

| 占位符 | plan 路径 | 说明 |
|---|---|---|
| `<output_database>` | `output_database` | 产物库 |
| `<sql_fragments.user_key_expr>` | `sql_fragments.user_key_expr` | |
| `<sql_fragments.valid_user>` | `sql_fragments.valid_user` | |
| `<sampling_sources.user_table>` | `sampling_sources.user_table` | |
| `<keys.label_column>` | `keys.label_column` | 列名 |
| `<label_pos_val>` | 同上 | `keys.label_column` 在 schema 中 valueType 为 Int* 时填 `1`，为 String 时填 `'1'` |
| `<label_neg_val>` | 同上 | Int* → `0`，String → `'0'` |
| `<sample_size>` | `sample_size` | |

---

### 主分支

只替换占位符，不改语句形状。先预检再建正式表。<必须>两条 LIMIT 不可改：正样本 = `CAST(<sample_size> / 5 AS UInt64)`，负样本 = `(SELECT count() * 4 FROM pos_limited)`，禁止直接写成 `<sample_size>` 字面量。</必须>

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_temp_sampled_users
ENGINE = MergeTree()
ORDER BY tuple()
AS
WITH
  -- ① 活跃用户：T0 前 lookback 窗口内有任意游戏行为的用户
  active AS (
    SELECT DISTINCT <sql_fragments.user_key_expr> AS user_key
    FROM {{source_database}}.<sampling_sources.activity_event>
    WHERE <sql_fragments.valid_user>
      AND <sql_fragments.pre_t0_lookback>
  ),
  -- ② 已转化用户：T0 前已命中 positive_label 的用户（需从候选池排除）
  converted AS (
    SELECT DISTINCT <sql_fragments.user_key_expr> AS user_key
    FROM {{source_database}}.<sampling_sources.conversion_event>
    WHERE <sql_fragments.valid_user>
      AND <sql_fragments.game_filter>
      AND <sql_fragments.positive_label>
      AND <sql_fragments.through_t0>                        -- 注意：≤ T0，不涉及未来
  ),
  -- ③ 候选池：活跃 减去 已转化 = 有行为但还没转化的用户
  pool AS (
    SELECT a.user_key
    FROM active AS a
    LEFT ANTI JOIN converted AS c
      ON a.user_key = c.user_key
  ),
  -- ④ 正样本：label 窗口 (T0, T0+N] 内为目标游戏转化了的用户 → label = 1（pos 取全部正样本，上限截断由 pos_limited 统一处理）
  pos AS (
    SELECT DISTINCT <sql_fragments.user_key_expr> AS user_key
    FROM {{source_database}}.<y_label.event_table>
    WHERE <sql_fragments.valid_user>
      AND <sql_fragments.game_filter>
      AND <sql_fragments.positive_label>                    -- 和 converted 用同一个谓词
      AND <sql_fragments.label_window>                      -- 注意：> T0，这是未来的行为
  ),
  -- ④.5 正样本上限截断：防止正样本超过 sample_size/5 挤掉全部负样本
  --     按哈希随机取 min(正样本池总量, sample_size / 5) 人
  pos_limited AS (
    SELECT user_key
    FROM pos
    ORDER BY cityHash64(user_key)
    LIMIT CAST(<sample_size> / 5 AS UInt64)
  ),
  -- ⑤ N1 负候选池（域内未转化）：候选池中排除正样本，不限量
  neg_N1_pool AS (
    SELECT 'N1' AS pop, p.user_key
    FROM pool AS p
    LEFT ANTI JOIN pos AS pos_ex
      ON p.user_key = pos_ex.user_key
  ),
  -- ⑥ N4 负候选池（高付费不付目标）：大盘付费用户中排除正样本，不限量
  neg_N4_pool AS (
    SELECT 'N4' AS pop, p.user_key
    FROM (
      SELECT <sql_fragments.user_key_expr> AS user_key
      FROM {{source_database}}.<y_label.event_table>
      WHERE <sql_fragments.valid_user>
        AND <sql_fragments.positive_label>
      GROUP BY user_key
    ) AS p
    LEFT ANTI JOIN pos AS pos_ex
      ON p.user_key = pos_ex.user_key
  ),
  -- ⑦ N5 负候选池（随机背景）：全量用户中排除正样本，不限量
  neg_N5_pool AS (
    SELECT 'N5' AS pop, <sql_fragments.user_key_expr> AS user_key
    FROM {{source_database}}.<sampling_sources.user_table> AS u
    LEFT ANTI JOIN pos AS pos_ex
      ON <sql_fragments.user_key_expr> = pos_ex.user_key
    WHERE <sql_fragments.valid_user>
  ),
  -- ⑧ 合并负池（仅 UNION  plan 的 negative_populations 中实际启用的群体；模板默认 N1/N4/N5，按 family 调整：付费侧重 N4，CTR 主用 N2，安装/留存/时长 主用 N3+N2。N2/N3 CTE 不在模板内，需按 resources/negative_samples.md 自行编写）
  neg_pool AS (
    SELECT user_key FROM neg_N1_pool
    UNION ALL
    SELECT user_key FROM neg_N4_pool
    UNION ALL
    SELECT user_key FROM neg_N5_pool
  ),
  -- ⑨ 按 1:4 比例截断负样本：取正样本数 × 4 人，哈希随机
  neg_sampled AS (
    SELECT user_key
    FROM neg_pool
    ORDER BY cityHash64(user_key)
    LIMIT (SELECT count() * 4 FROM pos_limited)
  ),
  -- ⑩ 合并：正样本(label=1) + 负样本(label=0)
  combined AS (
    SELECT user_key, toUInt8(1) AS label FROM pos_limited
    UNION ALL
    SELECT user_key, toUInt8(0) AS label FROM neg_sampled
  ),
  -- ⑪ 去重：一个人如果同时出现在 pos 和 neg 中，优先取 label = 1
  deduped AS (
    SELECT user_key, max(label) AS label
    FROM combined
    GROUP BY user_key
  )
-- ⑫ 安全兜底：总量已由 pos_limited + neg_sampled 精确控制，最终截到 sample_size
SELECT user_key, label
FROM deduped
ORDER BY cityHash64(user_key)
LIMIT <sample_size>;
```

### 占位符映射

| 占位符 | plan 路径 | 说明 |
|---|---|---|
| `<sql_fragments.user_key_expr>` | `sql_fragments.user_key_expr` | 用户键表达式 |
| `<sql_fragments.valid_user>` | `sql_fragments.valid_user` | 过滤用户键为空的行 |
| `<sql_fragments.game_filter>` | `sql_fragments.game_filter` | 游戏范围；cold_start 时已由 step1_2 更新 |
| `<sql_fragments.pre_t0_lookback>` | `sql_fragments.pre_t0_lookback` | T0 前 lookback 窗口 |
| `<sql_fragments.through_t0>` | `sql_fragments.through_t0` | ≤ T0 |
| `<sql_fragments.positive_label>` | `sql_fragments.positive_label` | 正样本谓词；pos 和 converted 共用 |
| `<sql_fragments.label_window>` | `sql_fragments.label_window` | T0 后 label 窗口 |
| `<sampling_sources.activity_event>` | `sampling_sources.activity_event` | active CTE 来源表 |
| `<sampling_sources.conversion_event>` | `sampling_sources.conversion_event` | converted CTE 来源表 |
| `<sampling_sources.user_table>` | `sampling_sources.user_table` | 用户表（N5 随机背景用） |
| `<y_label.event_table>` | `y_label.event_table` | pos / N4 CTE 来源表（label 事件表） |
| `<sample_size>` | `sample_size` | 总样本量（顶层参数） |

### 启用哪些 neg 群体

模板中 `neg_N1_pool` / `neg_N4_pool` / `neg_N5_pool` 为三个预置群体模板，提交前需按 plan 的 `negative_populations` 调整 `neg_pool`：

- 模板中已含 N1/N4/N5，仅保留 `code` 出现在 `negative_populations` 中的群体，删除未启用的整行 `SELECT ... FROM neg_Nx_pool`（连带上方 `UNION ALL`，注意首行无 UNION ALL）。
- 若 `negative_populations` 指定了模板以外的群体（如 N2/N3/N6），按 `resources/negative_samples.md` 口径编写对应 CTE 并加入 `neg_pool`。
- `neg_k` 值为各群体预估配额，仅作参考；SQL 中负样本总量由 `pos × 4` 自动计算，agent 不需要把 `neg_k` 写成 LIMIT。

各 family 默认组合见 `step1_0_sampling_plan.md` y-label 家族映射表。

---

## 产出

SQL 执行后生成 ClickHouse 中间表：

| 表名 | 列 | 说明 |
|---|---|---|
| `<output_database>.step1_temp_sampled_users` | `user_key` (用户键)、`label` (0/1) | 训练用户集，step1_4 用此表裁剪所有源表 |

不产出本地文件；表在 output_database 内。
