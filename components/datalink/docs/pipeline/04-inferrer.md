# Inferrer 模块实现文档

**源码路径：** `src/datalink/inferrer/`  
**输入：** `list[ColumnNode]` + `list[ColumnProfile]`（+ 部分模块需 `joinable_edges` / `DatasourceInfo`）  
**输出：** `list[Edge]`（B 类隐式边，confidence < 1.0）

四个 Inferrer 在 Pipeline Step 4 **顺序独立运行**，结果合并后统一按 `confidence_threshold` 过滤入库。

---

## 1. 总体比较策略

| Inferrer | 比较范围 | 跳过条件 |
|----------|----------|----------|
| JoinableInferrer | 跨表列对 | 同表、高基数、dtype 不兼容 |
| SynonymInferrer | 跨表列对 | 同表 |
| DistributionInferrer | 跨表列对 | 同表、dtype 不同 |
| CorrelationInferrer | join key 所在表的其他数值列对 | 非数值、无 joinable 边、无 sample_data |

默认两两比较复杂度约为 O(C²)，C 为列总数。

---

## 2. JoinableInferrer

**文件：** `joinable.py`  
**边类型：** `EdgeType.JOINABLE`  
**配置：** `joinable_overlap_threshold`（默认 0.1），`max_cardinality`（默认 900）

### 2.1 算法流程

```
1. profile_map = {column_id → ColumnProfile}
2. 按 table_id 分组 columns → table_groups
3. 对每对 (table_a, table_b), table_a ≠ table_b:
     对 col_a ∈ table_a, col_b ∈ table_b:
       if cardinality > max_cardinality → skip
       if _is_boolean_skip(profile) → skip
       if not _compatible_dtypes → skip
       overlap = _compute_overlap(profile_a, profile_b)
       if overlap >= threshold → 创建边，confidence = overlap
```

### 2.2 Boolean 列跳过（`_is_boolean_skip`）

布尔列（boolean、integer_boolean、float_boolean）的值域极小（如 {0, 1}），与任何其他布尔列 trivially overlap，产生的 joinable 边毫无查询意义。

| dtype | 是否跳过 | 原因 |
|------|---------|------|
| `boolean` | ✅ 跳过 | pandas 真布尔类型 |
| `integer_boolean` | ✅ 跳过 | BIGINT(0/1) 存储，实质布尔 |
| `float_boolean` | ✅ 跳过 | FLOAT(0.0/1.0) 存储，实质布尔 |
| `integer` | ❌ 不跳过 | 真整数列（标识符、数值） |
| `string` | ❌ 不跳过 | 字符串列（可能有意义的状态码） |

示例：`Charter (integer_boolean, values {0,1})` ↔ `Magnet (integer_boolean, values {0,1})` → overlap=100% → 但这是虚假的，因为布尔标记不会是 JOIN key。

### 2.3 Dtype 兼容矩阵（`_compatible_dtypes`）

| dtype_a | dtype_b | 兼容 |
|---------|---------|------|
| integer/float/integer_boolean/float_boolean | integer/float/integer_boolean/float_boolean | ✓ |
| string | string | ✓ |
| datetime/date | datetime/date | ✓ |
| integer/float/integer_boolean/float_boolean | string | ✓（标识符跨类型） |
| 其他组合 | | ✗ |

注意：boolean 类型的列虽然在兼容矩阵中可匹配，但会在 2.2 的 `_is_boolean_skip` 中被跳过，实际不会产生边。

### 2.3 重叠率公式（`_compute_overlap`）

```python
values_a = {top_values 的 value} ∪ {str(v) for v in sample_values}
values_b = {同上}
intersection = values_a & values_b
overlap_rate = |intersection| / min(|values_a|, |values_b|)
```

**含义：** 较小值集合中有多少比例出现在另一列的采样域中。  
**局限：** 基于 top-10 + 最多 5 个样本，非全量 Jaccard；高基数列被跳过。

### 2.4 边属性

```python
properties = {
    "overlap_rate": overlap_rate,
    "dtype_a": profile_a.dtype,
    "dtype_b": profile_b.dtype,
}
```

---

## 3. SynonymInferrer

**文件：** `synonym.py`  
**边类型：** `EdgeType.SEMANTIC_SYNONYM`

### 3.1 置信度融合（`_compute_confidence`）

输入信号：

- `type_match`：两列 `semantic_type` 均非 `unknown` 且相等
- `name_sim`：`_name_similarity(col_a.name, col_b.name)`
- `group_match`：两列名同属 `SYNONYM_GROUPS` 中某一组

决策表：

| 条件 | confidence |
|------|------------|
| type_match AND (name_sim > 0.5 OR group_match) | **0.95** |
| type_match only | **0.85** |
| group_match only | **0.80** |
| name_sim > 0.7 | **0.60** |
| name_sim > 0.5 | **0.40** |
| 其他 | **0.0**（不建边） |

### 3.2 列名相似度（`_name_similarity`）

```python
norm_a = name_a.lower().replace("_", "")
norm_b = name_b.lower().replace("_", "")

if norm_a == norm_b: return 1.0

if 子串包含:
    ratio = min(len) / max(len)
    return 0.5 + 0.5 * ratio   # [0.5, 1.0]

jaccard = |set(norm_a) ∩ set(norm_b)| / |set(norm_a) ∪ set(norm_b)|  # 字符集合
prefix_score = 公共前缀长度 / max(len)
return max(jaccard, prefix_score)
```

**注意：** 非标准 Levenshtein，是字符 Jaccard + 前缀 + 子串启发式。

### 3.3 同义词组（`SYNONYM_GROUPS`）

预定义 17 组常见列名同义词，例如：

```python
{"customer_id", "user_id", "client_id", "account_id", "person_id"}
{"amount", "value", "total", "sum", "price", "cost", "fee", "charge"}
```

匹配时列名转小写、`-` 换 `_` 后直接 membership 检查。

---

## 4. DistributionInferrer

**文件：** `distribution.py`  
**边类型：** `EdgeType.DISTRIBUTION_SIMILAR`  
**配置：** `similarity_threshold`（默认 0.5，类内硬编码）

### 4.1 分 dtype 路由（`_compute_similarity`）

```
if both numeric (integer/float) → _numeric_similarity
if both string               → _categorical_similarity
if both temporal (datetime/date) → _temporal_similarity
else → 0.0
```

### 4.2 数值相似度（`_numeric_similarity`）

**范围重叠：**

```python
range_a = (min_value, max_value)
range_b = (min_value, max_value)
overlap_start = max(range_a[0], range_b[0])
overlap_end = min(range_a[1], range_b[1])
if overlap_start > overlap_end: overlap_fraction = 0
else:
    overlap_fraction = (overlap_end - overlap_start) / (max(range) - min(range))
```

**均值相似度：**

```python
mean_similarity = 1.0 - |mean_a - mean_b| / max(|mean_a|, |mean_b|)
```

**最终：**

```python
similarity = overlap_fraction * 0.6 + mean_similarity * 0.4
```

### 4.3 分类相似度（`_categorical_similarity`）

```python
values_a = {value: fraction} from top_values
values_b = {value: fraction} from top_values
common = keys_a ∩ keys_b
weighted_overlap = Σ min(values_a[v], values_b[v]) for v in common
similarity = weighted_overlap / (sum(values_a) + sum(values_b)) * 2
             # 等价于 weighted_overlap / (total_weight / 2)
```

类似**加权分类分布重叠**（与 histogram 交集相关）。

### 4.4 时间相似度（`_temporal_similarity`）

MVP 简化实现：

```python
bin_similarity = min(len(hist_a), len(hist_b)) / max(len(hist_a), len(hist_b))
return bin_similarity * 0.5
```

仅比较直方图 bin 数量比例，**未**比较实际时间范围。

---

## 5. CorrelationInferrer

**文件：** `correlated.py`  
**边类型：** `EdgeType.CORRELATED`  
**配置：** `correlation_threshold`（默认 0.5）

### 5.1 核心设计

相关性分析的关键洞察：**correlated 边不应连接 join key 自身，而应连接 JOIN 后对齐的其他数值列**。

例如，若 `orders.customer_id ↔ users.id` 是 joinable 边（join key），则有意义的是 JOIN 后 `orders.amount` 和 `users.age` 是否线性相关——而不是 customer_id 和 id 之间的 trivial 相关（它们本就是同一组 ID）。

因此算法逻辑为：

```
对每条 joinable_edge (key_a ↔ key_b):
  1. 找到 key_a 所属表中其他数值列 (col_a2, col_a3, …)
  2. 找到 key_b 所属表中其他数值列 (col_b2, col_b3, …)
  3. 在 sample_data 上按 key_a = key_b 做 inner merge
  4. 对每对 (col_a2, col_b2) 计算真正的 Pearson r
  5. 若 |r| >= threshold → 创建 correlated 边 (col_a2 ↔ col_b2)
```

### 5.2 前置条件

| 条件 | 说明 |
|------|------|
| joinable 边 | 必须已有 joinable 边才能确定 join key |
| 数值列 dtype | 两列 dtype 均 ∈ `{integer, float}`，且非 join key 本身 |
| sample_data | 两表的 sample_data 都在 `DatasourceInfo` 中可用 |
| 对齐行数 ≥ 5 | merge 后至少 5 行有效数据才计算 Pearson |

### 5.3 Pearson 相关算法（`_compute_pearson`）

真正的样本级 Pearson 相关系数，不是范围比例代理：

```python
series_a = pd.to_numeric(merged[col_name_a], errors="coerce").dropna()
series_b = pd.to_numeric(merged[col_name_b], errors="coerce").dropna()

# Align indices after dropna
common_idx = series_a.index ∩ series_b.index
if len(common_idx) < MIN_ALIGNED_ROWS:  # 默认 5
    return None

corr = series_a.loc[common_idx].corr(series_b.loc[common_idx], method="pearson")
```

### 5.4 列名冲突处理（`_find_numeric_columns`）

`pd.merge` 使用 `suffixes=("_a", "_b")`。列名在两表冲突时自动加 suffix（如 `score_a` / `score_b`），不冲突时保持原名。`_find_numeric_columns` 同时检查带 suffix 和不带 suffix 的名称：

```python
suffixed_name = f"{col_name}{suffix}"   # e.g., "score_a"
if suffixed_name in merged.columns:
    merged_name = suffixed_name
elif col_name in merged.columns:        # 不冲突，保持原名
    merged_name = col_name
else:
    continue                            # 该列不在 merged 结果中
```

### 5.5 边属性

```python
properties = {
    "coefficient": corr,            # Pearson r 值
    "method": "pearson",            # 与实现一致
    "aligned_rows": len(merged),    # merge 后对齐行数
    "joinable_edge": edge.id,       # 哪条 joinable 边提供了 join key
    "join_key_a": key_a_id,         # 左侧 join key 列 ID
    "join_key_b": key_b_id,         # 右侧 join key 列 ID
}
```

### 5.6 Pipeline 调用方式

`CorrelationInferrer.infer()` 接收 `list[DatasourceInfo]`（而非单个 `DatasourceInfo`），因为跨表 merge 需要所有相关数据源的 sample_data 同时可用：

```python
# build_full 中：
correlated_edges = correlation_inferrer.infer(
    all_columns, all_profiles, joinable_edges, all_ds_infos
)
```

**`add_table` 中不调用 CorrelationInferrer**：增量添加新表时无法获取已有表的 sample_data（sample_data 未持久化到图数据库），因此无法做跨表 merge。这是一个已知限制——可通过将 sample_data 存入 metadata 表或重连数据源来解决。

---

## 6. Pipeline 中的合并与过滤

```python
all_inferred_edges = joinable + synonym + distribution + correlated

filtered_edges = [e for e in all_edges
                  if e.confidence >= config.confidence_threshold]  # 默认 0.3
```

显式边（FK、contains）confidence=1.0，始终保留。

---

## 7. add_table 时的增量推断

新表列与**已有列**合并后重新跑四个 Inferrer，但只保留**至少一端为新列**的边：

```python
new_column_ids = {c.id for c in new_columns}
def involves_new_column(edge):
    return edge.source_id in new_column_ids or edge.target_id in new_column_ids
```

避免重复写入已有列对之间的边（Storage 用 INSERT OR REPLACE，重复 ID 会覆盖）。

CorrelationInferrer 在 `add_table` 流程中**未**调用——因为无法获取已有表的 `sample_data`（未持久化到图数据库），无法做跨表 merge 来计算真正的 Pearson 相关性。

---

## 8. 边 ID 命名

| 类型 | ID 格式 |
|------|---------|
| joinable | `edge:joinable:{col_a}:{col_b}` |
| synonym | `edge:synonym:{col_a}:{col_b}` |
| distribution | `edge:dist_similar:{col_a}:{col_b}` |
| correlated | `edge:correlated:{col_a}:{col_b}` |

无向关系：source/target 顺序取决于双重循环的枚举顺序。
