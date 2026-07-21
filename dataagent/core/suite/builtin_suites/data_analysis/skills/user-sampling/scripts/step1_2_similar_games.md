# step1_2: similar_games（仅 cold_start）

**目的**：从游戏维表中找出与目标游戏同维度的其他游戏，写入 `game_scope.similar_games`，并更新 `sql_fragments.game_filter` 为引用临时表的写法。

## 前置

仅当 step1_1 判定 `mode = cold_start` 时执行；`regular` 和 `prelabeled` 跳过本步。

`read` `step1_0_table_schema.json` 与 `step1_0_sampling_plan.json`。列类型取自 schema。

| 用途 | plan 路径 |
|---|---|
| 游戏维度表 | `sampling_sources.game_dim` |
| 游戏键列 | `keys.game_key_default` |
| 相似维度列 | `keys.similar_dim` |
| 目标游戏 | `game_scope.target` |

缺 `keys.similar_dim` → 回 step1_0 补填。

---

## 拼 SQL 规则

1. 结构固定为下方模板（`DISTINCT` + 自连接取同维度游戏）；只替换占位符，不改语句形状。
2. 表名、列名、目标游戏名逐字来自 plan，禁止默写示例字面量。
3. 输出列名固定为 `game_id`，供写入 `game_scope.similar_games`。

---

## 模板

```sql
CREATE OR REPLACE TABLE <database>.step1_temp_similar_games
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT DISTINCT <game_key_expr> AS game_id
FROM <database>.<game_dim> AS g
INNER JOIN (
  SELECT <similar_dim>
  FROM <database>.<game_dim>
  WHERE <game_key_default> = '<game_scope.target>'
  LIMIT 1
) AS t ON g.<similar_dim> = t.<similar_dim>
WHERE <game_key_default> IS NOT NULL
  AND <game_key_default> != '<game_scope.target>';
```

| 占位符 | plan 路径 |
|---|---|
| `<game_key_expr>` | `sql_fragments.game_key_expr` |
| `<game_dim>` | `sampling_sources.game_dim` |
| `<similar_dim>` | `keys.similar_dim` |
| `<game_key_default>` | `keys.game_key_default` |
| `<game_scope.target>` | `game_scope.target` |

---

## 产出

### 1. 写入 similar_games

从 job 结果取出 `game_id` 列表，写入 `step1_0_sampling_plan.json`：
- `game_scope.similar_games` = `["<game_id_1>", "<game_id_2>", ...]`
- 若相似集为空，在 plan 里记 fallback（`game_scope.similar_games = []`），不阻塞。后续 `game_filter` 中的 `UNION ALL SELECT '<game_scope.target>'` 已兜底保留目标游戏，不影响采样。

### 2. 更新 game_filter

把 `sql_fragments.game_filter` 从目标游戏单点过滤更新为覆盖目标 + 相似游戏的范围过滤，格式固定为：

```
<game_key_default> IN (SELECT game_id FROM <database>.step1_temp_similar_games UNION ALL SELECT '<game_scope.target>' AS game_id)
```

即同时包含临时表中的相似游戏和目标游戏本身。**禁止**把 `similar_games` 列表展开成 `IN ('a','b','c',...)` 超长字面量。
