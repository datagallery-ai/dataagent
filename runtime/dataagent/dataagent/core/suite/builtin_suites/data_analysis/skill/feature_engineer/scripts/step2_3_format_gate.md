# Step2_3 列表字段分割门禁

> ⛔ **这是一个独立的 todo item，不可合并到 `1:N 聚合` 或 `SQL 建表` 的 todo 中。**
>
> **已知复发故障**：
> - 20260727：agent 完全跳过列表分割，5 个 delimiter 字段原样透传进 CSV
> - 20260727-2：加 5 独立 todo 后仍跳过——写完 SQL 直接 submit，无阻塞
> - 20260727-3：count-only（只写 length() 无 has()）
> - 061349/0728-5：每个字段只产出 1 个二元特征（`_sandbox` 但无 `_益智`、`_动作`），高频词项全部丢失
> - 0728-?：`list_detail_info` 的 10 个列表字段在 ld_agg CTE 中仅被当作普通列透传——splitByChar 完全未触发。根因：agent 的列表分割思维模型只覆盖 `user_info` 表，没有遍历 1:N 表的聚合 CTE
> - root cause：agent 生成到「有 has()」就停止，不生成全量 top-N 词项
> - **ClickHouse 23.8 多 LEFT JOIN**：扁平 `FROM w LEFT JOIN a ... LEFT JOIN b` 反复报 `Missing columns: 'usid'`（列实际存在）。诊断耗时约 40 分钟并创建 step2_3_test* / 物化 agg 表无效。根因是旧版本列解析限制。**立刻改写为每层恰好 1 个 JOIN 的嵌套子查询**，禁止 USING、禁止再建诊断表。
>
> **执行顺序（严格）：**
> 1. `read_file("scripts/step2_3_feature_aggregation.sql")` — 刷新合并 SQL 模板记忆
> 2. **先查询词项频次**：对每个待分割字段，通过 ClickHouse `submit_resource_job` 查询 Top-10 词项（见步骤 1.5）
> 3. 按模板规范生成 `step2_3_feature_aggregation_expanded.sql`（含 1:N 聚合 + 列表分割[二元展开] + 游戏维度 JOIN）
> 4. 立即执行以下 grep / 脚本硬门禁，不通过 → 回到步骤 2 修改 SQL → 重新检查
> 5. 执行 `scripts/step2_3_string_cast.md`：城市列 → `{col}_tier` 并删除原列（过拟合）；其余残留 String 先采样再 CAST
> 6. 门禁全部通过后执行 validation.sql（高基数 + **城市原列**）
> 7. **全部通过后**才能标记此 todo 为完成

---

## 步骤 1：读取合并 SQL 模板

```
read_file("scripts/step2_3_feature_aggregation.sql")
```

模板包含 `step2_3_wide_complete` 的唯一合法 SQL 结构：
- `/*__DERIVATION_CTES__*/` — 1:N 聚合 CTE + 列表词项统计 CTE
- `/*__DERIVED_SELECT_COLUMNS__*/` — 聚合列 + 列表二元特征列（`has(splitByChar(...), 'term')`）
- `/*__DERIVATION_JOIN_BLOCKS__*/` — 嵌套 JOIN（每层恰好 1 个 LEFT/CROSS JOIN；含游戏维度）
- `/*__CLEANING_EXCEPT_CLAUSE__*/` — 从 `step2_2_cleaning_report WHERE recommendation='DROP'` 查询展开

---

## 步骤 1.5：查询每个列表字段的 Top-N 词项频次（SQL 写作前必做）

> **为什么需要这一步**：二元特征必须基于**高频词项**，不能随机采样。061349 运行中 agent 对 `game_interest_play_u` 产出了 Top-10 全量二元特征（`_sandbox` + `_益智` + `_动作`...），0728-5 退化到 1 个——因为没有查询频次的引导。

对 `list_fields.txt` 中的每个字段，通过 ClickHouse `submit_resource_job` 提交。**FROM 子句取决于字段来源表类型：**

- **1:1 表字段**（如 `user_info` 的 `game_interest_play_u`）→ 从 `{output_database}.step2_2_wide_cleaned` 查询
- **1:N 表字段**（如 `list_detail_info` 的 `anime_list_90d`）→ 从**原始源表**查询（1:N 表字段尚未进入宽表，`step2_2_wide_cleaned` 中不存在）

```sql
-- 查询单个字段的词项频次（以 '#' 分隔为例，'^' 同理替换分隔符）
-- 1:1 表字段用 step2_2_wide_cleaned；1:N 表字段用原始源表
SELECT
    term,
    count() AS freq
FROM {source_database}.{source_table}   -- 根据字段来源表选择
ARRAY JOIN splitByChar('#', assumeNotNull({col})) AS term
WHERE term != ''
GROUP BY term
ORDER BY freq DESC
LIMIT 10
```

**二元特征生成规则**（基于频次查询结果）：

| 条件 | 规则 |
|------|------|
| 总词项数 ≤ 10 | **所有词项**都生成二元特征（一个不漏） |
| 总词项数 > 10 且 ≤ 100 | 取 **Top-10** 词项生成二元特征 |
| 总词项数 > 100 | 取 **Top-50** 词项生成二元特征 |
| NULL 或空字符串 | `is_empty_{col}=1`，所有二元特征为 0 |

**每个字段查询完后，将应产出的二元特征数保存到 `term_counts.txt`**（供步骤 2c2 对齐检查）：

```bash
# term_counts.txt 格式：每行 "字段名 M"（M = 应产出的二元特征数）
# 示例：
# game_interest_play_u 10
# sport_interest_ball_game_u 2
# care_notification_up 0  ← ID 列表字段，无分析价值
```

**⛔ ID 字段识别（频次查询后必做）**：

> 词项若为唯一标识符（app ID、广告主 ID、用户 ID 等），对模型训练**零信息增量**——不会跨用户传播。这类字段只生成 `list_length` + `is_empty`，**禁止**生成二元特征。
>
> 已知故障：5 个 ID 字段各生成 Top-50 二元特征（250 列），全部为无意义噪声。

对每个字段的频次查询结果，检查 Top-3 词项是否匹配以下任一模式：
- **纯数字**（≥ 10 位，如 `5064085579445075210`）
- **C-前缀数字**（如 `C827161690:59799、C30148978:442、C220320535`）
- 若匹配 → 该字段标记为 ID 字段，`term_counts.txt` 中写 0（仅 `_list_length` + `_is_empty`）

### 统一命名规约（一个列表字段拆分后产出的全部列）

以 `game_interest_play_u`（分隔符 `#`，10 个词项）为例：

| 列类型 | 命名格式 | 示例 | 数量 |
|--------|---------|------|------|
| **二元特征** | `{原字段名}_{英文词项}` | `game_interest_play_u_puzzle`、`game_interest_play_u_action` | = M（词项数） |
| **列表长度** | `{原字段名}_list_length` | `game_interest_play_u_list_length` | 1 |
| **是否为空** | `{原字段名}_is_empty` | `game_interest_play_u_is_empty` | 1 |

**1:N 表字段**：前缀加表别名，如 `ld_game_interest_preference_u_puzzle`、`ld_game_interest_preference_u_list_length`。

**ID 字段**（M=0）：仅 `_list_length` + `_is_empty`，无二元特征。

**词项英文映射**写入 `feature_derivation.md`（如 `puzzle → 益智`）。

**禁止行为**：
- 禁止凭字段名推断词项——必须从 ClickHouse 查询获取
- 禁止只取 1 个词项（除非该字段确实只有 1 个词项种类）。反面案例 0728-5：`game_interest_play_u` 只产出了 `_sandbox`
- 禁止用硬编码的用户 ID 或随机采样值作为二元词项

---

## 步骤 2：生成 expanded SQL 后执行 grep 硬门禁

对刚写入的 `step2_3_feature_aggregation_expanded.sql` 依次执行。

### 步骤 2a：提取待分割字段清单（全覆盖所有源表）

```bash
rg '\| (.+?) \| .+? \| "(#|\^)" \|' step2_0_source_data_analyze.md | sed 's/|.*//' | tr -d ' ' > list_fields.txt
cat list_fields.txt
```

记字段数为 **N**。——若 N = 0（无列表字段），门禁直接通过。

> ⛔ **交叉核对（必做，禁止跳过）**：逐行检查 `list_fields.txt` 中是否包含 1:N 表（如 `list_detail_info`）的列表字段。若 step2_0 的 `## 列表字段检测结果` 中标记了 `list_detail_info` 表的字段但未出现在 `list_fields.txt` 中（例如 grep 因正则差异漏捕），则**手动追加**这些字段到 `list_fields.txt`。
>
> **反面案例**：`list_detail_info` 的 10 个列表字段在 step2_0 中正确标记了 delimiter，但 agent 写 SQL 时完全遗漏——ld_agg CTE 将它们当作普通列透传。门禁必须确保所有源表的列表字段全量进入 `list_fields.txt`。

---

### 步骤 2b：验证 splitByChar — SQL 必须对每个待分割字段执行分割

```bash
grep -c "splitByChar" step2_3_feature_aggregation_expanded.sql
```

- 返回数 ≥ N → 继续
- 返回数 < N → **禁止 submit，回到步骤 2。**

---

### 步骤 2c：⛔ 反 count-only + 逐字段词项数对齐检查

> **已知复发故障**：
> - 20260727-3：只有 `length(splitByChar())`，无 `has()`
> - 0728-5：每个字段只产出 1 个 `has()`（`_sandbox` 但无 `_益智`、`_动作`），grep `≥ N` 被 17=17 精确通过
>
> **核心原则**：步骤 1.5 查了多少个词项，SQL 中就产出多少个二元特征——有几个词项就产出几个，一个不能少。

**2c1. 全量计数检查：**

```bash
grep -c "has(splitByChar" step2_3_feature_aggregation_expanded.sql
```

- 返回数 < N → **count-only 错误**。禁止 submit。
- 返回数 = N → **single-term 嫌疑**。进入 2c2
- 返回数 ≥ 2N → 进入 2c2 做逐字段对齐

**2c2. 逐字段词项数对齐（必做，禁止跳过）：**

> 步骤 1.5 的频次查询结果中，每个字段有 **M** 个词项（≤10 全取，>10 取 Top-10，>100 取 Top-50）
> SQL 中该字段的 `has(splitByChar(...), 'term')` 调用数必须 **= M**。

```bash
# 对照步骤 1.5 的频次查询结果，逐字段验证
# term_counts.txt 格式：每行 "字段名 M"（M = 应产出的二元特征数）
while read -r col M_rest; do
    col_name=$(echo "$col" | tr -d ' ')
    expected=$(echo "$M_rest" | awk '{print $1}')
    hits=$(grep -c "has(splitByChar.*$col_name" step2_3_feature_aggregation_expanded.sql)
    if [ "$hits" -ne "$expected" ]; then
        echo "MISMATCH: $col_name expected $expected binary features, found $hits"
    else
        echo "OK: $col_name = $hits/$expected"
    fi
done < term_counts.txt
```

- 任何 "MISMATCH" 输出 → **门禁不通过。** 该字段的二元特征数与词项数不对齐——少了或多了。**回到步骤 2 补全/修正。**
- 全部 "OK" → 通过

---

**2c3. ⛔ 二元特征列名规范**

> 反面案例：`gigp_term_yz`（拼音缩写，不可读）、`game_interest_style_u_3D`（字面量和编号混用）、`game_interest_play_u_益智`（中文列名）

```bash
# 检查是否有中文列名
grep "has(splitByChar" step2_3_feature_aggregation_expanded.sql | grep -oP 'AS\s+\K\S+' | grep -P '[一-龥]' && echo "CHINESE IN COLUMN NAME"
```

- "CHINESE IN COLUMN NAME" → **门禁不通过。改用 `{原字段名}_{英文词项}` 格式。**

核心规则：
- `{原字段名}_{英文词项}`，如 `game_interest_play_u_puzzle`（直接扩展原字段名，不缩写）
- 词项英文映射写入 `feature_derivation.md`（如 `puzzle → 益智`）

---

### 步骤 2d：验证 arrayJoin — 词项 > 100 的字段必须有 CTE 统计

```bash
grep -c "arrayJoin" step2_3_feature_aggregation_expanded.sql
```

- 若所有待分割字段词项 ≤ 100 → `arrayJoin` 命中数可为 0，跳过
- 若存在词项 > 100 的字段 → `arrayJoin` 命中数 ≥ 词项 > 100 的字段数。不满足 → 回到步骤 2

---

### 步骤 2e：⛔ 验证 *_terms CTE 被 JOIN — arrayJoin 统计不能是死代码

```bash
grep -oP '(\w+_terms)\s+AS' step2_3_feature_aggregation_expanded.sql | sed 's/ AS//' | sort -u > cte_names.txt
while read -r cte; do
    if ! grep -q "$cte" <(grep -i "JOIN\|FROM" step2_3_feature_aggregation_expanded.sql); then
        echo "DEAD CTE: $cte defined but never JOINed"
    fi
done < cte_names.txt
```

任何 "DEAD CTE" 输出 → **门禁不通过。**

---

### 步骤 2f0：⛔ 单 SQL 约束 — 禁止中间表流水线

> step2_3 必须在 **1 个 SQL** 中完成全部聚合 + 分割 + JOIN。禁止以下反模式：
> - 创建临时实体表（如 `CREATE TABLE step2_3_with_ld`、`step2_3_test*`、`dev_agg` 诊断表）再从中取数
> - 多 SQL 管道链（如 m1 → m2 → m3 → m4 → m5 串联提交）
> - **同一 FROM 子句多个 LEFT/INNER/CROSS JOIN**（ClickHouse 23.8 会误报 `Missing columns: 'usid'`）
> - `USING (usid)`（23.8 报 Multiple USING）
>
> 正确模式：1 条 `CREATE OR REPLACE TABLE step2_3_wide_complete AS ...`，JOIN 写成**嵌套子查询，每层恰好 1 个 JOIN**（见附录「ClickHouse 23.8 嵌套 JOIN」）。

**2f0a. 检查 SQL 是否为单条 CREATE：**

```bash
grep -c "CREATE.*TABLE" step2_3_feature_aggregation_expanded.sql
```

- = 1 → 通过（只有 1 条建表语句，正确）
- > 1 → **门禁不通过。SQL 含多条建表——agent 在创建中间表流水线。回到步骤 2 合并为单条 CTE 内聚。**

**2f0b. 检查是否使用 w.* EXCEPT (...)：**

```bash
grep -cF "EXCEPT" step2_3_feature_aggregation_expanded.sql
```

- ≥ 1 → 通过
- = 0 → **门禁不通过。回到步骤 2 改写。**

---

### 步骤 2f：⛔ 验证原字段已排除（step2_2_wide_cleaned 内的 1:1 表列表字段）

```bash
while read -r col; do
    rg "$col" step2_3_feature_aggregation_expanded.sql | grep -v "splitByChar\|ARRAY JOIN\|_cnt\|_list_length\|_is_empty\|has(" | head -1
done < list_fields.txt
```

- 若有输出 → 原字段未排除。回到步骤 2
- 全部无输出或仅在 splitByChar/ARRAY JOIN/has()/list_length/is_empty 中出现 → 通过

> ⛔ **仅检查 step2_2_wide_cleaned 的字段（w.* 或 w.col）。** 1:N 表列表字段的排除由步骤 2g 独立检查。

---

### 步骤 2g：⛔ 验证 1:N 表聚合 CTE 输出中无原始列表字段

> **目的**：1:N 表（如 `list_detail_info`）的列表字段在聚合 CTE（如 `ld_agg`）内部完成 splitByChar 后，CTE 输出中不得残留原始列表字段。本步骤独立于步骤 2f 是因为 1:N 表字段不在 `step2_2_wide_cleaned` 中，无法通过 `w.col` 引用检测。

```bash
# 2g1: 检查 1:N 表聚合 CTE 输出中是否残留原始列表字段
# 提取 list_fields.txt 中标注为 1:N 表来源的字段（如 ld_* 前缀），
# 检查它们在 expanded SQL 的 CTE SELECT 块中是否只以 splitByChar/has() 形式出现
while read -r col; do
    table=$(rg "\| $col \| (.+?) \|" step2_0_source_data_analyze.md -or '$1' | head -1)
    # 检查原始字段名（不带 _term_ / _list_length / _is_empty 后缀）是否出现在 CTE SELECT 输出中
    # 使用 rg 的多行匹配：在 CTE AS ( ... ) 块内搜索
    if rg -U "\b${col}\b[^_]|AS\s+\w*${col}\s*$" step2_3_feature_aggregation_expanded.sql | grep -v "splitByChar\|has(\|_term_\|_list_length\|_is_empty\|ARRAY JOIN" > /dev/null 2>&1; then
        echo "LEAK: raw list field '$col' found in CTE output (not split)"
    fi
done < list_fields.txt
```

更简化的检查（推荐）：grep `_list_length` 和 `_is_empty` 的总命中数。

```bash
# 2g2: 验证 list_length + is_empty 成对出现
# 每个列表字段应产出一对 _list_length + _is_empty
for col in $(cat list_fields.txt); do
    has_length=$(grep -c "${col}_list_length" step2_3_feature_aggregation_expanded.sql)
    has_empty=$(grep -c "${col}_is_empty" step2_3_feature_aggregation_expanded.sql)
    if [ "$has_length" -eq 0 ] || [ "$has_empty" -eq 0 ]; then
        echo "MISSING AUX: $col has _list_length=$has_length, _is_empty=$has_empty (both must be >= 1)"
    fi
done
```

- 任何 "LEAK" 或 "MISSING AUX" 输出 → **门禁不通过。** 回到步骤 2
- 全部通过 → 继续

---

### 步骤 2h：⛔ 游戏维度表完整性 — 所有维度表必须出现

> 已知故障：agent 在 v1~v4 正确写了 CROSS JOIN，重构最终版时忘了复制。

从 `step2_0_source_data_analyze.md` 动态提取维度表清单（概览行分类列为 `维度表` 的表名）：

```bash
# 提取所有分类为"维度表"的表名
rg '\| .+? \| .+? \| .+? \| .+? \| 维度表 \|' step2_0_source_data_analyze.md | awk '{print $1}' | tr -d '|' > dim_tables.txt
cat dim_tables.txt
```

若 `dim_tables.txt` 为空（本次数据无维度表）→ 跳过此步骤。

```bash
while read -r tbl; do
    grep -q "$tbl" step2_3_feature_aggregation_expanded.sql || echo "MISSING: $tbl"
done < dim_tables.txt
```

- 任何 "MISSING" → **门禁不通过。回到步骤 2 补全 CROSS JOIN。**

---

### 步骤 2i：⛔ ClickHouse 23.8 JOIN 嵌套硬门禁

> 扁平多 JOIN 会在 23.8 上循环报 `Missing columns: 'usid'`。收到该错误时**不要**建 `step2_3_test*`，**立刻**按附录改成每层 1 个 JOIN。

对刚写入的 `step2_3_feature_aggregation_expanded.sql` 执行：

```bash
python skill/feature-engineer/scripts/step2_3_check_join_nesting.py step2_3_feature_aggregation_expanded.sql
```

- exit 0 → 通过
- 非 0 → **门禁不通过。** 按报错把每个 FROM 改成至多 1 个 JOIN 的嵌套子查询后重跑。禁止 USING，禁止诊断表。

---

### 步骤 2j：⛔ 城市原列不得进入宽表（过拟合）

地名会过拟合。`step2_3_wide_complete` / CSV 只允许 `{col}_tier`，禁止 `city` / `city_name` / `*城市*` 原列。

`string_cast_plan.txt` 中规则为 `city_tier` 的列名必须同时满足：

1. 最外层 `SELECT` 用 `EXCEPT ({col}, ...)` 丢掉原列
2. 出现 `{col}_tier`（由 `step2_3_city_tier_sql.py` 生成，禁止手写截断版 IN 列表）
3. 建表后 `step2_3_validation.sql` **第三条**查询返回 0 行

CSV 导出后可用：

```bash
python skill/feature-engineer/scripts/step2_3_city_tier_sql.py --check-csv step2_4_wide_userfiltered.csv
```

exit 非 0 → 表头仍有 `city` 原列，门禁失败。

---

### 门禁通过后的下一步

全部步骤（2b + 2c + 2d + 2e + 2f0a + 2f0b + 2f + 2g + 2h + **2i** + **2j**）通过后 → submit SQL 建表 → 执行 `step2_3_validation.sql` 门禁（含城市原列查询）。

---

## 为什么需要这个门禁

step2_0 有 format_gate.md——这是所有运行中唯一被证明有效的防御手段。

**0719 里程碑**：agent 首次产出真正的二元特征（Top-10 per field）。
**0728-5 里程碑**：agent 首次通过 EXCEPT 删除全部 17 个原始列表字段，high_cardinality_check 返回 0。

当前缺陷：每个字段只 1 个二元特征——门禁 2c 从 `≥ N` 升级为逐字段计数检查解决此问题。

---

## 附录：1:N 表列表字段拆分模板

> **适用场景**：1:N 表（如 `list_detail_info`）在聚合 CTE 中包含列表字段，必须在 CTE 内部完成 splitByChar 二元展开，不得透传原始列表字段。

**正面示例**（假设 `list_detail_info` 的 `anime_list_90d` 标记 `delimiter:"#"`，词项为 `"热血"`、`"悬疑"`、`"恋爱"`）：

```sql
ld_agg AS (
    SELECT
        usid,
        -- 原始列表字段已删除，替换为二元特征
        max(has(splitByChar('#', assumeNotNull(anime_list_90d)), '热血')) AS ld_anime_list_90d_term_热血,
        max(has(splitByChar('#', assumeNotNull(anime_list_90d)), '悬疑')) AS ld_anime_list_90d_term_悬疑,
        max(has(splitByChar('#', assumeNotNull(anime_list_90d)), '恋爱')) AS ld_anime_list_90d_term_恋爱,
        max(length(splitByChar('#', assumeNotNull(anime_list_90d)))) AS ld_anime_list_90d_list_length,
        max(anime_list_90d IS NULL OR anime_list_90d = '') AS ld_anime_list_90d_is_empty,
        -- ... 其他非列表字段正常聚合 ...
        max(app_pref_count) AS ld_app_pref_count
    FROM source_db.list_detail_info
    GROUP BY usid
)
```

> 词项查询：1:N 表列表字段的频次查询 `FROM` 子句指向**原始源表**（如 `source_db.list_detail_info`），非 `step2_2_wide_cleaned`。

**反面示例（禁止）：**

```sql
-- BAD: 列表字段原样透传，未拆分
ld_agg AS (
    SELECT
        usid,
        max(anime_list_90d) AS ld_anime_list_90d,     -- 原样透传！
        max(movies_list_90d) AS ld_movies_list_90d,    -- 原样透传！
        max(app_pref_count) AS ld_app_pref_count
    FROM source_db.list_detail_info
    GROUP BY usid
)
```

---

## 附录：ClickHouse 23.8 嵌套 JOIN（Missing columns: 'usid'）

> **已知限制**：23.8 在同一 FROM 中多个 LEFT JOIN 引用同一键列时列解析失败，物化真实表也一样。
> 验证过的解法只有：**每层子查询恰好 1 个 JOIN**。CROSS JOIN 维度表单独一层。

**反面（禁止，会报 Missing columns: 'usid'）：**

```sql
SELECT w.* EXCEPT (...), a.x, b.y, d.z
FROM step2_2_wide_cleaned AS w
LEFT JOIN cte_a AS a ON w.usid = a.usid
LEFT JOIN cte_b AS b ON w.usid = b.usid
CROSS JOIN game_dim AS d
```

**正面（必须）：**

```sql
SELECT j2.*, d.z
FROM (
    SELECT j1.*, b.y
    FROM (
        SELECT w.* EXCEPT (...), a.x
        FROM {{output_database}}.step2_2_wide_cleaned AS w
        LEFT JOIN cte_a AS a ON w.<user_id> = a.<user_id>
    ) AS j1
    LEFT JOIN cte_b AS b ON j1.<user_id> = b.<user_id>
) AS j2
CROSS JOIN game_dim AS d
```

维度 CTE 内部若要拼多张维表，同样每层只 JOIN 一次，禁止 `FROM gi LEFT JOIN gf ... LEFT JOIN gb`。

收到 `Missing columns: '<user_id>'` 时：禁止 `step2_3_test*`、禁止 `USING`、禁止继续扁平重试；按本附录改写后重新 submit 这一条 CREATE。
