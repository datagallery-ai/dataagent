# Extractor 模块实现文档

**源码路径：** `src/datalink/extractor/tabular.py`  
**输入：** `DatasourceInfo`  
**输出：** `(list[TableNode], list[ColumnNode], list[Edge])`

Extractor 将 Connector 输出的原始元数据转换为图谱**结构层**节点和**显式边**（A 类边），不涉及数据值分析。

---

## 1. 核心类：TabularExtractor

唯一公开方法：

```python
def extract(self, datasource_info: DatasourceInfo) -> tuple[list[TableNode], list[ColumnNode], list[Edge]]
```

---

## 2. ID 生成规则

`generate_id(*parts)` 用冒号拼接，保证**确定性**（同一数据源重复构建 ID 不变）：

| 节点/边 | ID 格式 | 示例 |
|---------|---------|------|
| TableNode | `table:{source}:{table_name}` | `table:./data:orders` |
| ColumnNode | `column:{source}:{table}:{column}` | `column:./data:orders:customer_id` |
| contains 边 | `edge:contains:{table_id}:{col_id}` | — |
| foreign_key 边 | `edge:fk:{src_table}:{src_col}:{tgt_table}:{tgt_col}` | — |

其中 `source = table_info.source or config.path or config.connection_string or config.name or "unknown"`。`table_info.source` 由 Connector 写入（文件源为路径字符串，数据库源为连接字符串），优先使用以确保 ID 确定性。

---

## 3. 逐表处理流程

对每个 `TableInfo`：

### 3.1 创建 TableNode

```python
TableNode(
    id=table_id,
    name=table_info.name,
    source=source_name,
    row_count=table_info.row_count or 0,
    properties={
        "schema_name": table_info.schema_name,
        "comment": table_info.comment,
    },
)
```

`column_ids` 在创建所有 Column 后回填。

### 3.2 创建 ColumnNode

对每个 `ColumnInfo`：

```python
ColumnNode(
    id=col_id,
    name=col_info.name,
    table_id=table_id,
    dtype=col_info.dtype,
    comment=col_info.comment,
    properties={
        "nullable": col_info.nullable,
        "is_primary_key": col_info.is_primary_key,
        "default_value": str(col_info.default_value) or "",
    },
)
```

此阶段 `semantic_type`、`profile_id` 为空，由 Pipeline Step 3 在 Profiling 后回填。

### 3.3 创建 contains 边

每列一条，方向 **Table → Column**：

```python
Edge(
    source_id=table_id,
    target_id=col_id,
    type=EdgeType.CONTAINS,
    confidence=1.0,  # 默认值
)
```

### 3.4 创建 foreign_key 边

对 `table_info.foreign_keys` 中每条 FK：

```python
fk_source_id = generate_id("column", source, fk.source_table, fk.source_column)
fk_target_id = generate_id("column", source, fk.target_table, fk.target_column)

Edge(
    source_id=fk_source_id,
    target_id=fk_target_id,
    type=EdgeType.FOREIGN_KEY,
    confidence=1.0,
    properties={"constraint_name": fk.constraint_name},
)
```

**方向：** FK 列（source）→ 被引用列（target）。

**悬空边处理：** FK 涉及的列若不在本次 extract 范围内（跨 schema 或跨数据源），边不会丢弃而是存入 `pending_edges` 表，标记 `missing_endpoints`（如 `["target"]`）。当目标数据源后续通过 `add_table` 加入图谱时，Pipeline 自动调用 `resolve_pending_edges()`，将两端节点都存在的 pending 边"接合"到 `edges` 主表，参与正常检索和遍历。

---

## 4. Pipeline 中的属性回填

Extractor 本身不写 DB；Pipeline 在 Profiling 后会更新 ColumnNode / TableNode 的 `properties`，供 Storage 序列化：

**ColumnNode 追加：**

```python
col.properties["dtype"] = col.dtype
col.properties["semantic_type"] = col.semantic_type   # 来自 Profile
col.properties["table_id"] = col.table_id
col.properties["profile_id"] = col.profile_id
col.properties["comment"] = col.comment
```

**TableNode 追加：**

```python
table.properties["source"] = table.source
table.properties["row_count"] = table.row_count
table.properties["column_ids"] = table.column_ids
```

Storage 的 `_row_to_node()` 从 `properties` JSON 反序列化回强类型字段。

---

## 5. 数据流

```
DatasourceInfo
    tables[].columns[]  ──► ColumnNode[]
    tables[]            ──► TableNode[]
    tables[].foreign_keys[] ──► Edge[FOREIGN_KEY]
    (implicit)          ──► Edge[CONTAINS] (每列一条)
         │
         ▼
Pipeline Step 3: 用 profile_map 更新 semantic_type / profile_id
         │
         ▼
GraphStorage.add_nodes_batch() + add_edges_batch()
```

---

## 6. 复杂度

设 T 表、C 总列数、F FK 数：

- 时间：O(T × avg_cols + F)
- 空间：O(T + C + F + C)（contains 边数 = C）

无跨表比较，纯元数据映射。

---

## 7. 设计决策

| 决策 | 理由 |
|------|------|
| ID 含 source 前缀 | 多数据源合并时避免列名冲突 |
| FK 仅来自 schema | 文件源无 FK，留给 Inferrer |
| 不在 Extractor 做 Profiling | 职责分离，Profile 需 sample_data |
| properties 冗余存储 typed 字段 | SQLite 单表存所有节点，JSON 存变长属性 |
