# Step2 部署特征契约（口径与校验）

> ⛔ **契约不手写。** `step2_2_deployment_feature_contract.json` 与
> `step2_2_feature_aggregation_expanded.sql` 由 `step2_2_driver.py finalize` 从
> `step2_2_expand_sql.py` 的同一份内存特征定义直接写出；禁止在 SQL 执行后根据 Markdown
> 或 SQL 文本反向猜测，禁止手工拼契约。聚合函数与丢弃判定取自
> `references/aggregation_policy.json`（唯一来源）。

契约记录如何在全量源表上复现 `step2_4_wide_userfiltered.csv` 中的特征，供 NL2SQL
确定性渲染。生成后必须跑 `step2_2_validate_deployment_contract.py` 并满足结构检查，
才能在 receipt 登记。

## 结构总览

顶层字段：`contract_version`（固定 `1`）、`source_artifact`（本次通过门禁的 expanded SQL 文件名）、
`entity`（`grain` / `base_relation_plan` / `entity_key` / `label_column`）、`relation_plans`、`features`、`validation`。

- `entity.entity_key` / `label_column` 用 Step2 最终宽表的实际列名，不得假定标签固定叫 `label`。
- `relation_plans` 的 key 是稳定 plan id（不得用 `agg_1` 这类依赖遍历顺序的名字）。
- `features` 的 key 与 Step2_4 CSV 特征列一致（排除用户键与 label）。

## relation plan 的三种 `kind`

| kind | 用途 | 关键约束 |
|------|------|---------|
| `entity` | 用户基表，每份契约恰好一个，与 `entity.base_relation_plan` 一致 | 可 LEFT/INNER JOIN 已在 step2_1 验证不膨胀的 1:1 表 |
| `user_aggregation` | 按用户聚合后 LEFT JOIN 回基表；1:1 附属表也用此类型（`any(...)` 保持用户粒度） | 必须返回用户粒度 |
| `scalar` | 与用户无关、CROSS JOIN 到全体用户的单行特征组（维度特征） | 必须始终返回一行 |

约束：每个 plan 的 alias 唯一；`source.table` 与 `joins[].table` 必须是 `step1_output_meta.json`
中的物理源表；JOIN 类型只允许 `LEFT` / `INNER` / `CROSS`（`CROSS` 不写 `on`，其余必须写 `on`）；
多物理表关系必须写成 `joins`，不得交给下游猜成 `UNION ALL`。

## feature 约束

- `expression` 是可直接放入所属 plan SELECT 的 warehouse SQL 表达式。
- 引用只允许本 plan 声明的 alias；禁止引用 `w` / `dim_agg` / `*_agg` 等其他 scope 的临时 alias。
- `source_columns` 必须完整列出表达式读取的物理字段并与 alias 的表一致；仅 `count()` 等不读字段的表达式允许空列表。
- `output_type` 用 Step2 宽表实际输出类型；`null_policy.kind` 只允许 `preserve` / `fill`（`fill` 必须给 JSON 标量 `value`）。
- 表达式 / 过滤 / JOIN 条件只允许一个运行时参数 `{{target}}`（NL2SQL 用 `step1_sample_stats.json.target` 替换）；
  禁止把库名、表名、列名或任意 SQL 片段参数化。

## 校验（step2_4 CSV 导出后）

```bash
python skill/feature-engineer/scripts/step2_2_validate_deployment_contract.py \
  --contract step2_2_deployment_feature_contract.json \
  --schema step1_output_meta.json \
  --wide-csv step2_4_wide_userfiltered.csv \
  --expanded-sql step2_2_feature_aggregation_expanded.sql \
  --cleaning-report step2_3_cleaning_report.json
```

- `--cleaning-report` 提供 step2_3 的 DROP 清单：契约特征缺席最终 CSV 时，只有命中该清单才算「被清洗」而非「丢失」。
- 检查器以原子替换写入 `validation.structural_validation`；该状态**只能由本脚本写**，禁止手工伪造通过。
- 结构检查只确认契约完整；SQL 执行正确性由 NL2SQL source trial 验证（`runtime_validation.performed=false`）。
- 通过后才把契约登记到 receipt；不通过先修正并重跑，仍未通过则不在 receipt 登记该契约，且不得改动 Step2 表、文档、训练 CSV 与 receipt 其他内容。
