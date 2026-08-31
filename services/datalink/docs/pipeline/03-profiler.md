# Profiler 模块实现文档

**源码路径：** `src/datalink/profiler/tabular.py`  
**输入：** `DatasourceInfo`（必须含 `sample_data`）  
**输出：** `list[ColumnProfile]`

Profiler 对每列采样数据计算**统计指纹（ColumnProfile）**，是 Inferrer 与 Mapper 的核心数据基础。

---

## 1. 入口：profile_datasource

```
对每个 table in datasource_info.tables:
    sample_rows = sample_data[table_name]
    if empty → warning + skip
    df = pd.DataFrame(sample_rows)
    对每个 column in table.columns:
        if col_name not in df.columns → skip
        profile_column(col_id, col_name, df[col_name], comment)
```

`col_id` 与 Extractor 一致：`column:{source}:{table}:{column}`  
`profile.id` = `profile:{col_id}`

---

## 2. profile_column 计算流程

对单列 `series`（Pandas Series）：

### 2.1 基础统计

```python
non_null = series.dropna()
total_count = len(series)
null_count = series.isna().sum()
null_rate = null_count / total_count
cardinality = non_null.nunique()
unique_rate = cardinality / total_count
dtype = _classify_dtype(series)
value_patterns = _detect_patterns(non_null)
semantic_type = _classify_semantic_type(name, dtype, patterns, non_null)
```

### 2.2 数值列扩展统计

条件：`pd.api.types.is_numeric_dtype(non_null)`

```python
numeric_values = pd.to_numeric(non_null, errors="coerce").dropna()
min, max, mean, std, median = ...
distribution_histogram = _numeric_histogram(numeric_values, bins=10)
```

直方图：NumPy `histogram(values, bins=10)` → `[{bin_start, bin_end, count}, ...]`

### 2.3 字符串列扩展统计

条件：`dtype == "string"`

```python
min_length, max_length, avg_length = str_values.str.len().agg(...)
```

### 2.4 高频值与样本

```python
top_values = _compute_top_values(non_null, top_n=10)
# Counter → [{value, count, fraction}, ...]

sample_values = non_null.sample(min(5, len), random_state=42).tolist()
```

`random_state=42` 保证可复现。

---

## 3. 类型检测算法（`_classify_dtype`）

优先级链：

| 步骤 | 条件 | 结果 |
|------|------|------|
| 1 | `is_bool_dtype` | `boolean` |
| 2 | `is_integer_dtype` + distinct ≤ 2 + values ⊆ {0,1} | `integer_boolean` |
| 3 | `is_integer_dtype`（其他） | `integer` |
| 4 | `is_float_dtype` + distinct ≤ 2 + values ⊆ {0.0,1.0} | `float_boolean` |
| 5 | `is_float_dtype`（其他） | `float` |
| 6 | `is_datetime64_any_dtype` | `datetime` |
| 7 | object/string：90%+ 可转 numeric，全整数 + distinct ≤ 2 + values ⊆ {0,1} | `integer_boolean` |
| 8 | object/string：90%+ 可转 numeric，全整数 | `integer` |
| 9 | object/string：90%+ 可转 numeric | `float` |
| 10 | object/string：90%+ 可转 datetime | `datetime` |
| 11 | object/string | `string` |
| 12 | 其他 | `unknown` |

**`integer_boolean` 和 `float_boolean` 的设计意图：**

数据库中布尔列通常存为 BIGINT(0/1)，pandas 读出后 dtype 为 `integer`。如果直接标记为 `integer`，下游 JoinableInferrer 会误判这些列的值域高度重叠，产生无意义的 joinable 边（如 `Charter ↔ Charter School (Y/N)`）。

通过检测整数列仅含 {0, 1}，标记为 `integer_boolean`，下游可以正确地：
- semantic_type → `boolean_flag`（字段角色分类更准确）
- JoinableInferrer → 跳过 boolean 类型列（避免虚假 JOIN 推断）

---

## 4. 值模式检测（`_detect_patterns`）

仅对 string/object 列，从最多 20 个随机样本（`random_state=42`）匹配正则：

| pattern_name | 正则（摘要） | 触发条件 |
|--------------|-------------|----------|
| email_pattern | `^[\w.+-]+@[\w-]+\.[\w.-]+$` | 匹配率 > 80% |
| url_pattern | `^https?://` | > 80% |
| date_pattern | `^\d{4}-\d{2}-\d{2}$` | > 80% |
| datetime_pattern | ISO datetime 前缀 | > 80% |
| uuid_pattern | UUID 格式 | > 80% |
| phone_pattern | 电话格式 | > 80% |
| zip_pattern | 美国邮编 | > 80% |

返回匹配的 pattern_name 列表，供字段角色分类使用。

---

## 5. 字段角色分类（`_classify_semantic_type`）

三级决策树：

### 5.1 值模式优先（最可靠）

```python
pattern_to_semantic = {
    "email_pattern": "email_address",
    "url_pattern": "url",
    "date_pattern": "date",
    "datetime_pattern": "datetime",
    "uuid_pattern": "uuid",
    "phone_pattern": "phone_number",
    "zip_pattern": "postal_code",
}
```

### 5.2 列名规则（`SEMANTIC_TYPE_RULES`）

`SEMANTIC_TYPE_RULES` 包含两类规则（约 30+ 条 `(regex, semantic_type)` 对）：

**值模式规则**（前 8 条，对列值匹配）：

- `^[\w.+-]+@[\w-]+\.[\w.-]+$` → `email_address`
- `^\+?\d{1,3}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{1,4}[-.\s]?\d{1,9}$` → `phone_number`
- `^https?://[\w./-]+$` → `url`
- 等等

**列名规则**（其余条目，对**小写列名**匹配）：

- `.*_id$`, `^id$` → `identifier`
- `.*_amount$`, `^amount$` → `monetary_value`
- `.*_email$` → `email_address`
- `.*_date$`, `.*_time$` → `timestamp`

完整列表见源码 `SEMANTIC_TYPE_RULES`。

### 5.3 Dtype 兜底

- `boolean` / `integer_boolean` / `float_boolean` → `boolean_flag`
- `datetime` → `timestamp`
- `unique_rate > 0.9` 且 dtype 为 integer/string → `identifier`
- 否则 → `unknown`

---

## 6. Pipeline 中的 Profile 回写

```python
profile_map = {p.column_id: p for p in all_profiles}
for col in all_columns:
    profile = profile_map.get(col.id)
    if profile:
        col.semantic_type = profile.semantic_type
        col.profile_id = profile.id
        col.properties[...] = ...  # 供 Storage 序列化
```

Profile 本身通过 `GraphStorage.add_profiles_batch()` 存入 `column_profiles` 表（properties 为除 id/column_id 外全部字段的 JSON）。

---

## 7. ColumnProfile 字段用途

| 字段 | 使用者 |
|------|--------|
| `semantic_type` | SynonymInferrer, LLMMapper prompt |
| `top_values`, `sample_values` | JoinableInferrer 值域重叠 |
| `dtype` | Joinable/Distribution 类型过滤 |
| `cardinality` | Joinable 跳过高基数列（>10000） |
| `min/max/mean`, `distribution_histogram` | DistributionInferrer, CorrelationInferrer |
| `sample_values` | LLMMapper prompt |

---

## 8. 复杂度与限制

- 每列：O(n) 扫描采样行，n ≤ sample_size（默认 1000）
- 全库：O(总列数 × n)
- **限制：**
  - 无 sample_data 的表/列被跳过
  - 字段角色规则基于英文列名启发式，中文列名易归为 `unknown`
  - top_values 仅反映采样分布，非全量精确统计
