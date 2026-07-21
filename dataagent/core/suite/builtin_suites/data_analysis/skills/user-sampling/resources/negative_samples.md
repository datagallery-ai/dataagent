# 口径：negative_samples（多群体负样本）

**目的**：按 SKILL §5 构造语义区分的多群体负样本，而非将候选池内全部非正样本混合为单一群体。

**公共约束**：所有群体排除正样本；每群体输出**不限量**（`ORDER BY cityHash64` 后不加 LIMIT），负样本总量由 `step1_3` 中 `neg_sampled` 的全局 `LIMIT (SELECT count() * 4 FROM pos_limited)` 统一控制。`neg_k` 为各群体预估配额，仅作参考，不可写成 LIMIT。

**ClickHouse 方言**：
排除集合统一用：
```sql
LEFT ANTI JOIN <excluded_set> AS x ON outer.<user_id> = x.<user_id>
```

等价写法：`outer.<user_id> NOT IN (SELECT <user_id> FROM <excluded_set>)`（子查询内避免产出 NULL 键）。**禁止**再写相关 `NOT EXISTS`。

**表/列来源**：plan 的 `sampling_sources` + `keys`（语义服务确认后 step1_0 填入）。

**参数**：`{{database}}`、`T0`、`label_window_days`、`game_scope`；SQL 片段用 plan 的 `sql_fragments`。

各群体模板仅在所需逻辑角色经语义层解析成功时可选。`negative_populations` 记录实际采用的来源表、表级键映射与口径；缺失的事件流不由其它固定表名替代。

| 代号 | 群体 | 构造口径 |
|---|---|---|
| N1 | 域内未转化 | 来自 `pool`，`LEFT ANTI JOIN pos` |
| N2 | 曝光未转化 | 曝光目标/相似游戏但未点击/无下游转化 |
| N3 | 高活跃不玩目标 | 近 90 天行为量高分位，窗口内不碰目标/相似游戏 |
| N4 | 高付费不付目标 | 付费大盘用户，窗口内不为目标/相似游戏付费 |
| N5 | 随机背景 | 全体用户哈希抽样，排除正样本 |
| N6 | 兴趣错配 | 画像与目标游戏明显不同，且非正样本 |

**按目标点名（SKILL §5）**：付费必含 N4；点击/CTR 以 N2 为主；安装/留存/时长以 N3+N2 为主；至少一 hard + 一 N5。
`scripts/step1_3_build_training_set.md` 的 `combined` **仅 UNION plan 中选用的 `neg_*` CTE**。

---

## N1 域内未转化

```sql
neg_N1 AS (
  SELECT p.user_key
  FROM pool AS p
  LEFT ANTI JOIN pos AS pos_ex
    ON p.user_key = pos_ex.user_key
)
```

## N2 曝光未转化

```sql
neg_N2 AS (
  SELECT <canonical_user_key_exposure> AS user_key
  FROM {{database}}.<exposure_event> AS e
  LEFT ANTI JOIN pos AS pos_ex
    ON <canonical_user_key_exposure> = pos_ex.user_key
  WHERE <valid_user_key_predicate_exposure>
    AND <game_filter_predicate_exposure>
    AND <negative_exposure_window_predicate>
    AND <exposure_not_converted_predicate>
)
```

## N3 高活跃不玩目标

```sql
neg_N3 AS (
  SELECT h.user_key
  FROM (
    SELECT <canonical_user_key_activity> AS user_key, count() AS act_cnt
    FROM {{database}}.<activity_source>
    WHERE <valid_user_key_predicate_activity>
      AND <pre_t0_lookback_predicate_activity>
    GROUP BY user_key
  ) AS h
  LEFT ANTI JOIN (
    SELECT DISTINCT <canonical_user_key_activity> AS user_key
    FROM {{database}}.<activity_source>
    WHERE <valid_user_key_predicate_activity>
      AND <game_filter_predicate_activity>
      AND <label_window_predicate_activity>
  ) AS touched
    ON h.user_key = touched.user_key
  LEFT ANTI JOIN pos AS pos_ex
    ON h.user_key = pos_ex.user_key
  WHERE h.act_cnt >= (
    SELECT quantileExact(0.9)(act_cnt)
    FROM (
      SELECT count() AS act_cnt
      FROM {{database}}.<activity_source>
      WHERE <valid_user_key_predicate_activity>
        AND <pre_t0_lookback_predicate_activity>
      GROUP BY <canonical_user_key_activity>
    ) AS q
  )
)
```

## N4 高付费不付目标

```sql
neg_N4 AS (
  SELECT p.user_key
  FROM (
    SELECT <canonical_user_key_pay> AS user_key
    FROM {{database}}.<pay_booking_event>
    WHERE <valid_user_key_predicate_pay>
      AND <positive_pay_predicate>
    GROUP BY user_key
  ) AS p
  LEFT ANTI JOIN pos AS pos_ex
    ON p.user_key = pos_ex.user_key
  LEFT ANTI JOIN (
    SELECT DISTINCT <canonical_user_key_pay> AS user_key
    FROM {{database}}.<pay_booking_event>
    WHERE <valid_user_key_predicate_pay>
      AND <game_filter_predicate_pay>
      AND <positive_pay_predicate>
      AND <label_window_predicate_pay>
  ) AS paid_target
    ON p.user_key = paid_target.user_key
)
```

## N5 随机背景

```sql
neg_N5 AS (
  SELECT <canonical_background_user_key> AS user_key
  FROM {{database}}.{{background_user_source}} AS u
  LEFT ANTI JOIN pos AS pos_ex
    ON <canonical_background_user_key> = pos_ex.user_key
  WHERE <valid_background_user_key_predicate>
)
```

`{{background_user_source}}` 是语义层解析出的用户全集来源；可为用户维表，也可为业务源表用户键的去重子查询。

## N6 兴趣错配

```sql
neg_N6 AS (
  SELECT <canonical_background_user_key> AS user_key
  FROM {{database}}.{{background_user_source}} AS u
  LEFT ANTI JOIN pos AS pos_ex
    ON <canonical_background_user_key> = pos_ex.user_key
  WHERE <valid_background_user_key_predicate>
    AND <interest_mismatch_predicate>
)
```

N6 仅适用于 `{{background_user_source}}` 含有语义层确认的用户兴趣属性；否则该群体不进入 plan。

**每群体产出**：`SELECT user_key, toUInt8(0) AS label, '<代号>' AS neg_pop`（`neg_pop` 仅统计用，不进投影表）。口径、来源表、抽样量写入 `step1_0_sampling_plan.json.negative_populations`。
