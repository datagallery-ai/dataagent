# step1_3：按训练集投影源表

`prelabeled` 和 `regular` 都执行本步。读取 schema 和 plan。先把当前仓的表清单与 `source_table_inventory.tables` 对齐：仓里多出的表补进 schema 和 plan；plan 里有而仓里没有的表，写阻塞说明并停下。

然后为 `projections[]` 的每一张源表，在 `output_database` 建立同名表。过滤用户时只读 `step1_temp_sampled_users`。下面三个模板按 `projections[].type` 选用，编译后再提交。

## user_table

`regular` 在源列之外追加训练集的 `label`，并按用户键保留一行：

```sql
<LOGICAL_MATERIALIZE target="{{output_database}}.<table>">
<LOGICAL_DEDUPLICATE keys="src.<user_key>" order_by="<源表的稳定排序>">
SELECT src.*, s.label
FROM {{source_database}}.<table> AS src
JOIN {{output_database}}.step1_temp_sampled_users AS s
  ON src.<user_key> = s.user_key
WHERE <valid_user>
```

`prelabeled` 沿用源表自己的标签列，筛选值使用 plan 的 `label_values`：

```sql
<LOGICAL_MATERIALIZE target="{{output_database}}.<table>">
<LOGICAL_DEDUPLICATE keys="src.<user_key>" order_by="<源表的稳定排序>">
SELECT src.*
FROM {{source_database}}.<table> AS src
JOIN {{output_database}}.step1_temp_sampled_users AS s
  ON src.<user_key> = s.user_key
WHERE <valid_user>
  AND src.<label_column> IN (<label_values.negative>, <label_values.positive>)
```

## user_keyed

保留源表列，只留下训练用户的行：

```sql
<LOGICAL_MATERIALIZE target="{{output_database}}.<table>">
SELECT t.*
FROM {{source_database}}.<table> AS t
WHERE <valid_user>
  AND t.<user_key> IN (
    SELECT user_key FROM {{output_database}}.step1_temp_sampled_users
  )
```

## entity_keyed

用 plan 的 `sql_fragments.entity_filter` 保留目标实体的行：

```sql
<LOGICAL_MATERIALIZE target="{{output_database}}.<table>">
SELECT t.*
FROM {{source_database}}.<table> AS t
WHERE <sql_fragments.entity_filter>
  AND t.<entity_key> IS NOT NULL
```

## 建完后的核对

每张表建完就核对该表：

| type | 通过条件 |
|---|---|
| `user_table` | 去重用户数等于 `step1_temp_sampled_users` 的行数 |
| `user_keyed` | 去重用户数小于或等于训练用户数 |
| `entity_keyed` | 记录产出行数和源表行数 |

全部建完后，用 `<LOGICAL_COUNT_TABLES>` 统计 `output_database` 里不带 `step1_` 前缀的表。这个数量等于 `inventory_check.table_count` 后，读取 `scripts/step1_4_stats.md`。
