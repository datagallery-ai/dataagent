# Step2 部署特征契约

`step2_3_deployment_feature_contract.json` 是 Step2 新增的机器可读交付物。它与
`step2_3_feature_aggregation_expanded.sql` 必须由同一份内存特征定义生成，不得在 SQL
执行完成后根据 Markdown 或 SQL 文本反向猜测。

该增量产物不改变任何 Step2 表、特征、清洗、聚合、过滤或 CSV 导出逻辑。它只记录如何在
全量源表上复现 `step2_4_wide_userfiltered.csv` 中的特征，供 NL2SQL 确定性渲染。

## 顶层结构

```json
{
  "contract_version": 1,
  "source_artifact": "step2_3_feature_aggregation_expanded.sql",
  "entity": {
    "grain": "user",
    "base_relation_plan": "user_base",
    "entity_key": "usid",
    "label_column": "label"
  },
  "relation_plans": {},
  "features": {},
  "validation": {}
}
```

- `contract_version` 固定为 `1`。
- `source_artifact` 固定指向本次实际提交并通过门禁的 expanded SQL。
- `entity.entity_key` 使用 Step2 最终宽表中的用户键列名。
- `entity.label_column` 使用 Step2 最终宽表中的实际标签列名，不得假定固定叫 `label`。
- `relation_plans` 的 key 是稳定 plan id；不得使用 `agg_1` 等依赖遍历顺序的名称。
- `features` 的 key 必须与 Step2_4 CSV 的特征列一致，排除用户键和 label。
- `validation.structural_validation` 由固定检查脚本写入，Agent 不得手工伪造通过状态。该检查
  只确认契约结构完整，不判断任意 ClickHouse 表达式的完整语义；运行正确性由 NL2SQL
  source trial 验证。

## relation plan

支持三种 `kind`：

### entity

用户基表。每份契约恰好一个，且必须与 `entity.base_relation_plan` 一致。
`entity_key_expression` 是基表 plan 中的用户键表达式。该 plan 可以 LEFT/INNER JOIN 已在
Step2_1 验证为不膨胀的 1:1 用户表，使这些表上的直接特征也能确定性回放。

```json
{
  "kind": "entity",
  "source": {"table": "user_info", "alias": "u"},
  "joins": [],
  "filters": [],
  "entity_key_expression": "u.usid"
}
```

### user_aggregation

按用户聚合后 LEFT JOIN 回用户基表。1:1 附属表也用此类型，通过 `any(...)` 保持用户粒度。

```json
{
  "kind": "user_aggregation",
  "source": {"table": "game_booking_pay_info", "alias": "bp"},
  "joins": [],
  "filters": [],
  "entity_key_expression": "bp.usid"
}
```

### scalar

与用户无关、最终 CROSS JOIN 到全部用户的单行特征组，适用于目标游戏维度特征。

```json
{
  "kind": "scalar",
  "source": {"table": "game_info", "alias": "gi"},
  "joins": [
    {
      "type": "LEFT",
      "table": "game_feedback",
      "alias": "gf",
      "on": "gi.game_id = gf.game_id"
    },
    {
      "type": "LEFT",
      "table": "game_brand",
      "alias": "gb",
      "on": "gi.game_id = gb.game_id"
    }
  ],
  "filters": ["gi.game_name = {{target_game}}"]
}
```

每个 plan 中的 alias 必须唯一。`source.table` 和每个 `joins[].table` 都必须是
`step1_output_meta.json` 中存在的物理源表。JOIN 类型仅允许 `LEFT`、`INNER`、`CROSS`；
`CROSS` 不写 `on`，其他类型必须写 `on`。

多物理表关系必须写成 `joins`，不得将表列表交给下游猜成 `UNION ALL`。

## feature

```json
{
  "bp_total_pay_amount": {
    "relation_plan": "booking_pay_by_user",
    "expression": "sum(toFloat64OrZero(bp.pay_amount))",
    "source_columns": [
      {"alias": "bp", "column": "pay_amount"}
    ],
    "output_type": "Float64",
    "null_policy": {"kind": "fill", "value": 0}
  }
}
```

要求：

- `expression` 是可直接放入所属 plan SELECT 的 ClickHouse 表达式。
- 所有 `alias.column` 必须引用所属 plan 中声明的 alias；禁止引用 `w`、`gda`、`*_agg`
  等其他 SQL scope 的临时 alias。
- `source_columns` 必须完整列出表达式读取的物理字段，并与 alias 对应的物理表一致；只有
  `count()` 等确实不读取字段的表达式允许空列表。
- `output_type` 使用 Step2 宽表实际输出类型。
- `null_policy.kind` 仅允许 `preserve` 或 `fill`；`fill` 必须提供 JSON 标量 `value`。
- `entity` plan 应保持用户粒度，`scalar` plan 应始终返回一行。固定脚本不使用聚合函数
  白名单推断这些语义，Agent 必须依据生成 SQL 时的同一份定义保证它们成立。

列表字段展开示例：

```json
{
  "game_interest_theme_u_wuxia": {
    "relation_plan": "user_base",
    "expression": "has(splitByChar('#', assumeNotNull(u.game_interest_theme_u)), '武侠')",
    "source_columns": [
      {"alias": "u", "column": "game_interest_theme_u"}
    ],
    "output_type": "UInt8",
    "null_policy": {"kind": "preserve"}
  }
}
```

## 参数

表达式、过滤条件和 JOIN 条件只允许一个运行时参数：`{{target_game}}`。NL2SQL 会使用
`step1_sample_stats.json.target_game` 作为 SQL 字符串字面量替换。禁止数据库名、表名、列名
或任意 SQL 片段参数化。

## 生成与校验

1. 生成 expanded SQL 时，先在本地 Python 数据结构中维护 relation plans 和 feature specs。
2. 用同一份结构生成 SQL 动态块和本 JSON；禁止分别手写两套定义。
3. Step2_3 原有 SQL/格式/后端门禁全部通过后写 JSON。
4. Step2_4 CSV 导出完成后运行：

```bash
python skill/feature-engineer/scripts/step2_3_validate_deployment_contract.py \
  --contract step2_3_deployment_feature_contract.json \
  --schema step1_output_meta.json \
  --wide-csv step2_4_wide_userfiltered.csv \
  --expanded-sql step2_3_feature_aggregation_expanded.sql
```

检查器以原子替换方式写入：

```json
{
  "validation": {
    "structural_validation": {"passed": true},
    "runtime_validation": {
      "performed": false,
      "expected_stage": "nl2sql_source_trial"
    }
  }
}
```

结构检查通过时才把本契约登记到 receipt。检查失败时，Agent 可以修正新增契约并重跑；仍未
通过则不发布契约，但不得阻止 Step2 原有表、文档、训练 CSV 和 receipt 定稿。真正的 SQL
语法、函数、类型及 JOIN 执行正确性在 NL2SQL 的 source database 限量试算中验证。
