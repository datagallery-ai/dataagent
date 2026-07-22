# step1_1: explore_target_game

<入口规则>仅当 `mode!="prelabeled"` 时执行；`mode=="prelabeled"` 跳过本步</入口规则>

**目的**：数一下**目标游戏**在 label 窗口 `(T0, T0+N]` 里有多少**正样本用户**，据此写入 plan 的 `mode`（`regular` 或 `cold_start`）。

## 前置

step1_0 已完成，`read` `step1_0_table_schema.json` 与 `step1_0_sampling_plan.json`。缺字段回 step1_0。

表名 / 列名 / 列类型均取自 `step1_0_table_schema.json`；SQL 片段来自 plan。

---

## 拼 SQL 规则

1. 结构固定为下方**模板**（`uniqExact` + 四段 WHERE）；只替换占位符，不改语句形状。
2. 每个 WHERE 条件**逐字**来自 plan 的 `sql_fragments` / `y_label.event_table`。谓词里的取值（如 `entity_flag = '…'`）必须是 step1_0 里用 SQL 从实表查出来的真实取值，禁止编造。
3. `positive_label` 与 `event_table` 使用 plan 中当前 family 对应的片段，模板不变。

---

## 模板

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_temp_positive_probe
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT uniqExact(<user_key_expr>) AS positive_user_cnt
FROM {{source_database}}.<y_label.event_table>
WHERE <valid_user>
  AND <game_filter>
  AND <positive_label>
  AND <label_window>;
```

| 占位符 | plan 路径 |
|---|---|
| `<user_key_expr>` | `sql_fragments.user_key_expr` |
| `<y_label.event_table>` | `y_label.event_table` |
| `<valid_user>` | `sql_fragments.valid_user` |
| `<game_filter>` | `sql_fragments.game_filter` |
| `<positive_label>` | `sql_fragments.positive_label`（与 family 对应的片段） |
| `<label_window>` | `sql_fragments.label_window`（原样展开） |

---

## 两种 mode（本步只选其一）

| `mode` | 含义 | 条件 | 下一步 |
|---|---|---|---|
| `regular` | 目标游戏正样本充足，仅围绕目标游戏采样 | `positive_user_cnt` ≥ `cold_start_threshold`（默认 500） | 跳过 step1_2，直接 step1_3 |
| `cold_start` | 目标游戏正样本不足，需借相似游戏扩池 | `positive_user_cnt` < `cold_start_threshold` **且通过下方兜底验证** | 必须走 step1_2 |

---

## 正样本不足时的兜底验证

当 `positive_user_cnt < cold_start_threshold` 时，先**不要直接记 cold_start**——用一条**宽松查询**确认是真的少还是条件写错了：

```sql
SELECT count() AS loose_cnt
FROM {{source_database}}.<y_label.event_table>
WHERE <valid_user>
  AND <game_filter>
  AND <label_window>;
```

即去掉 `<positive_label>`，仅保留 valid_user + game_filter + label_window。

**判断**：

- **宽松 count ≥ positive_user_cnt 且宽松 count 本身也很低**（如也在阈值以下）→ 确认数据确实少，记 `cold_start`。
- **宽松 count 明显高于 positive_user_cnt**（如成千上万）→ 大概率是 `positive_label` 写错了。此时查一下枚举列（`SELECT DISTINCT entity_flag, status FROM … WHERE valid_user + game_filter + label_window`），找出真实取值，回 step1_0 改正 `sql_fragments.positive_label` 后重跑本步。
- **宽松 count = 0** → 说明不是 positive_label 的问题，是 game_filter 或 label_window 有问题，回 step1_0 检查。

---

## 产出

- 中间表 `step1_temp_positive_probe`（含 `positive_user_cnt`）
- 更新 `step1_0_sampling_plan.json` 的 `mode`：
  - `positive_user_cnt` ≥ `cold_start_threshold` → `"regular"`
  - 否则且兜底验证通过 → `"cold_start"`
