# Step2_2 列表字段分割与聚合门禁口径

> ⛔ **门禁由 `step2_2_driver.py gate` 一次跑完（2a~2l）**，命令与提交顺序见 `SKILL.md` 的 step2_2 段。
> 门禁编号、含义与失败字段由 `gate` 逐条打印（并写入 `step2_2_feature_gate.json`），
> **不要自行编号或对表**。

不通过 → 修改 `schema_resolution.json` / `step2_0_profile_dump.json` / `list_fields.txt` / `string_cast_plan.txt`
等输入后重跑 `finalize`（**不要手改 expanded SQL**）。

---

## 词项选择（`finalize` 内部执行，禁止手工重算）

档位阈值只写在 `references/aggregation_policy.json` 的 `term_top_by_total`，**不得在别处另写数值**；
实际命中档位由 `finalize` 打印。词项频次由 `prepare` 生成的词项查询 SQL 收集（字段多时自动拆为多个 `step2_2_term_query_<i>.sql` 逐个提交，避免单条 UNION ALL 结果超过 MCP 结果体量上限触发非 JSON 解析失败）：
`FROM` 按字段所属表类型自动选择 `step2_1_wide_joined`（1:1 表字段）或原始源表（1:N 表字段）。

词项数上限、`take=all` 兜底等规则全部读自该 JSON，此处不重复。

### 统一命名规约（一个列表字段拆分后产出的全部列）

| 列类型 | 命名格式 | 示例 | 数量 |
|--------|---------|------|------|
| **二元特征** | `{宽表列名}_term_{拼音}` | `<list_col>_term_rexue`、`<list_col>_term_xuanyi` | = M（词项数） |
| **列表长度** | `{宽表列名}_list_length` | `<list_col>_list_length` | 1 |
| **是否为空** | `{宽表列名}_is_empty` | `<list_col>_is_empty` | 1 |

1:N 表字段的宽表列名带表名前缀（如 `dwd_pay_event_<list_col>_term_daoju`）；1:1 表字段保留 step2_1 的宽表拼写。
词项→列名后缀映射由脚本生成并写入 `step2_2_feature_derivation.md`，禁止手写。

**展开位置（下沉边界）**：

| 列表字段所属表 | 展开位置 | 表达式形态 |
|---------------|---------|-----------|
| **1:N 行为表** | 该表的聚合 CTE 内（`GROUP BY` 前） | `max(has(splitByChar(...)))` / `max(length(...))` / `max(toUInt8(...))` |
| **1:1 表** | 最外层 | `has(splitByChar(...))` / `length(...)` / `toUInt8(...)`，**无 `max()`** |

1:1 表没有可聚合粒度（已由 step2_1 拍平进 `step2_1_wide_joined`），没有 CTE 可下沉；
其列表原列走最外层 `EXCEPT(...)` + 表达式。地理 `{col}_tier` 与采样 CAST 同理落在最外层
（维度表列在 CROSS JOIN 层）。

**禁止行为**（门禁会拦）：
- 禁止凭字段名推断词项——必须从 `<warehouse>` 的真实频次查询获取
- 禁止只取 1 个词项（除非该字段确实只有 1 个词项种类）
- 禁止用硬编码的用户 ID 或随机采样值作为二元词项
- 禁止任何中文出现在 `AS` 后的别名（中文只允许作为 `has(splitByChar(...), '中文')` 的参数）

---

## 门禁覆盖的错误类型

gate 逐条打印编号与失败字段；本表只列「现象 → 该改哪个输入」，便于定位：

| 现象 | 该改的输入 |
|------|-----------|
| 残留列表原列、1:N 表列表字段未在 CTE 内分割 | `list_fields.txt`（缺字段）或 `finalize` 前的 dump |
| 二元特征数与词项数不一致、只写 `length()` 不写 `has()` | 词项频次 payload（`raw_terms.json`），非 SQL |
| 列名含中文 | 输入字段名与词项（脚本转拼音，报错说明输入异常） |
| 聚合函数与 policy 不符、丢弃列缺 reason | `references/aggregation_policy.json` 与列画像（禁止手改 SQL） |
| 扁平多 LEFT JOIN / 诊断表残留 | 输入 schema（禁止手改 expanded SQL） |
| 地理原列进入宽表 | `references/scene.yaml` 的 `geo_columns`（禁止手工保地名） |
| 1:N 派生 String 列未转换 | `string_cast_plan.txt`（走 `sample` → `string-plan` 链路） |

`gate` 同时校验 `step2_2_validation_expanded.sql` 的 `/*__LIST_FIELD_NAMES__*/`、
`/*__GEO_COLUMN_MATCH__*/`、`/*__ALLOWED_STRING_COLUMNS__*/` 三个块已绑定且无 `/*__...__*/` 残留；
这些块只允许由 `finalize` 绑定，**禁止手工替换**。

---

### 建表 / 仓内 validation / 本地 gate 的顺序

`finalize` 写出建表与 validation 的 `jobs`，并把 `after_collect` 指向 `gate`。正确顺序：

1. 按 `step2_2_run.json` 的 `jobs` 提交建表 SQL → 逐条提交 validation（`finalize` 已把
   `step2_2_validation_expanded.sql` 按 `FORMAT JSON;` 边界自动拆为
   `step2_2_validation_1/2/3.sql`，每文件一条门禁 SELECT：用户键 / 高基数 / 地理原列；
   各自返回 0 行才算仓内通过）
2. 再跑清单里的 `gate`（本地 2a~2l）
3. `gate` 通过后会清除 `after_collect`，进入 step2_3

---

## 附录：1:N 表列表字段拆分模板

> 以下为示例；示例未覆盖的规则见上文。
> ⛔ 1:N 表的列表字段必须在聚合 CTE 内部完成 splitByChar 二元展开，不得透传原始列表字段；
> `step2_2_expand_sql.py` 已按本模板确定性生成，禁止手工改写。

**正面示例**（假设 `<n_to_one_table>` 的 `<list_col>` 标记 `delimiter:"#"`，词项为中文 `"热血"`、`"悬疑"`、`"恋爱"`；列名一律用其拼音，禁止中文；`<n_to_one_table>` 是实际表名占位）：

```sql
<n_to_one_table>_agg AS (
    SELECT
        <user_id>,
        -- 原始列表字段已删除，替换为二元特征；has() 第二参数是数据取值（保留中文），AS 后列名禁止中文
        max(has(splitByChar('#', assumeNotNull(<list_col>)), '热血')) AS <n_to_one_table>_<list_col>_term_rexue,
        max(has(splitByChar('#', assumeNotNull(<list_col>)), '悬疑')) AS <n_to_one_table>_<list_col>_term_xuanyi,
        max(has(splitByChar('#', assumeNotNull(<list_col>)), '恋爱')) AS <n_to_one_table>_<list_col>_term_lianai,
        max(length(splitByChar('#', assumeNotNull(<list_col>)))) AS <n_to_one_table>_<list_col>_list_length,
        max(toUInt8(<list_col> IS NULL OR toString(<list_col>) = '')) AS <n_to_one_table>_<list_col>_is_empty,
        -- ... 其他非列表字段按 aggregation_policy.json 聚合 ...
        sum(<metric_col>) AS <n_to_one_table>_<metric_col>,
        count() AS <n_to_one_table>_row_count
    FROM {{output_database}}.<n_to_one_table>
    GROUP BY <user_id>
)
```

> 列表字段名前缀是**表名**（`<n_to_one_table>_`），不是 `ld_`。
> 词项查询：1:N 表列表字段的频次查询 `FROM` 子句指向**原始源表**，非 `step2_1_wide_joined`（由 `prepare` 自动选择）。

**反面示例（禁止）：**

```sql
-- BAD: 列表字段原样透传，未拆分
<n_to_one_table>_agg AS (
    SELECT
        <user_id>,
        max(<list_col>) AS <n_to_one_table>_<list_col>,     -- 原样透传！
        sum(<metric_col>) AS <n_to_one_table>_<metric_col>
    FROM {{output_database}}.<n_to_one_table>
    GROUP BY <user_id>
)
```

---

## 附录：部分 warehouse 版本 嵌套 JOIN（Missing columns: '<user_id>'）

> 同一 FROM 中多个 LEFT JOIN 引用同一键列时列解析失败，物化真实表也一样。
> 唯一可行写法：**每层子查询恰好 1 个 JOIN**。CROSS JOIN 维度表单独一层。

**反面（禁止，会报 Missing columns: '<user_id>'）：**

```sql
SELECT w.* EXCEPT (...), a.x, b.y, d.z
FROM step2_1_wide_joined AS w
LEFT JOIN cte_a AS a ON w.<user_id> = a.<user_id>
LEFT JOIN cte_b AS b ON w.<user_id> = b.<user_id>
CROSS JOIN entity_dim AS d
```

**正面（必须）：**

```sql
SELECT j2.*, d.z
FROM (
    SELECT j1.*, b.y
    FROM (
        SELECT w.* EXCEPT (...), a.x
        FROM {{output_database}}.step2_1_wide_joined AS w
        LEFT JOIN cte_a AS a ON w.<user_id> = a.<user_id>
    ) AS j1
    LEFT JOIN cte_b AS b ON j1.<user_id> = b.<user_id>
) AS j2
CROSS JOIN entity_dim AS d
```

维度 CTE 内部若要拼多张维表，同样每层只 JOIN 一次，禁止 `FROM gi LEFT JOIN gf ... LEFT JOIN gb`。

收到 `Missing columns: '<user_id>'` 时：禁止 `step2_2_test*`、禁止 `USING`、禁止继续扁平重试；按本附录改写后重新 submit 这一条 CREATE。
