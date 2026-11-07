# Pipeline 编排模块实现文档

**源码路径：** `src/datalink/builder/pipeline.py`  
**职责：** 串联 Connector → Extractor → Profiler → Inferrer → Mapper → Storage，提供全量构建与增量更新。

---

## 1. 入口函数

### `_connect_datasource(config: DatasourceConfig) -> DatasourceInfo`

```python
if config.type == DATABASE:
    connector = DatabaseConnector(config)
elif config.type in (CSV, PARQUET):
    connector = FileConnector(config)
else:
    raise ValueError(...)

connector.connect()
ds_info = connector.get_datasource_info()
connector.disconnect()
return ds_info
```

每次连接**独立** open/close，不在 Pipeline 实例上缓存 Connector。

---

## 2. init_build — 首次构建

### 2.1 执行顺序

```
Step 1  对每个 datasource_config → _connect_datasource → all_ds_infos[]
Step 2–5 _compute_pipeline(all_ds_infos) — 纯计算，返回所有 artifacts
        (tables, columns, profiles, concepts, entities,
         structural_edges, inferred_edges, semantic_edges)
Step 6  storage.clear_all()
        _store_pipeline_result(result) — 批量写入 nodes, edges, profiles
        set_metadata("build_type", "init")
```

---

## 2b. rebuild — 重建图谱

从已有 TableNode 元数据重建，不需要用户提供数据源信息。支持三种模式：

### 2b.1 三种重建模式

| 模式 | 覆盖范围 | LLM 调用 | 适用场景 |
|------|----------|-----------|----------|
| `full`（默认） | 完整 pipeline + embedding 向量 | ✅ 有 | 数据大幅变化、全面刷新 |
| `vec` | 仅 embedding 向量 | ❌ 无 | 更换了 embedding 模型 |
| `profile` | 统计值 + 依赖统计的推断边 | ❌ 无 | 数据量变化但概念结构不变 |

### 2b.2 mode=full — 安全性保证

**rebuild 采用 compute-first → clear-last 模式**，确保旧数据安全：

1. 先从已有 TableNode 读取元数据并连接数据源
2. 调用 `_compute_pipeline` 纯计算所有结果（不写 DB）
3. **只有 pipeline 成功后才 `clear_all` + `_store_pipeline_result`**
4. 如果 pipeline 失败（LLM 超时等），旧数据完好保留，可再次 rebuild 恢复

执行顺序：

```
Step 1  读取所有 TableNode → 按 source 分组（同一数据库连接的多张表只需一次连接）
Step 2  对每个 source 组：
        - 用 TableNode 的 source + source_type 构造 DatasourceConfig
        - _connect_datasource(config) → DatasourceInfo
        - 过滤 DatasourceInfo.tables 只保留已有表名的表
Step 3–5 _compute_pipeline(all_ds_infos) — 纯计算，不写 DB
        返回所有 artifacts（tables, columns, profiles, concepts, entities,
        structural_edges, inferred_edges, semantic_edges）
Step 6  仅在 Step 3–5 成功后：
        storage.clear_all()
        _store_pipeline_result(result) — 批量写入
        set_metadata("build_type", "rebuild")
Step 7  _build_embeddings_from_pipeline_result(result) — 如果 embedding 已配置
```

分组逻辑：同一 `source` 的多张表只需一次连接，避免重复连接数据库。

### 2b.3 mode=vec — 向量重建

```
Step 1  检查 embedding 配置是否可用（model + api_key）
Step 2  _collect_nodes_with_profiles() — 从 DB 读取所有节点 + 关联 profile
Step 3  node_to_searchable_text(node, profile) — 为每个节点生成可检索文本
Step 4  embedding_service.compute_embeddings(texts) — 批量计算向量
Step 5  storage.clear_embeddings() — 清除旧向量
        storage.add_embeddings_batch([(node_id, model_name, embedding, text)])
        storage.set_metadata("embedding_model", model_name)
```

不涉及任何图谱数据修改，不调用 LLM。

### 2b.4 mode=profile — 统计重建 + 推断边重建

```
Step 1  读取所有 TableNode → 按 source 分组
Step 2  对每个 source 组：
        - 用 TableNode 元数据构造 DatasourceConfig
        - _connect_datasource → DatasourceInfo
        - TabularProfiler.profile_datasource → 新 profiles
Step 3  storage.add_profiles_batch(new_profiles) — 更新统计值
        _update_column_properties(column_nodes, new_profiles) — 更新节点属性
        _update_table_properties + add_nodes_batch — 更新表节点属性
Step 4  storage.remove_edges_by_types([JOINABLE, DISTRIBUTION_SIMILAR,
        SEMANTIC_SYNONYM, CORRELATED]) — 删除旧推断边
Step 5  用更新后的 profiles + sample_data 重新推断：
        - JoinableInferrer → new_joinable
        - SynonymInferrer → new_synonym
        - DistributionInferrer → new_distribution
        - CorrelationInferrer → new_correlated (需要 sample_data)
Step 6  _store_edges(new_inferred_edges, all_node_ids) — 存储新推断边
```

**为什么 profile rebuild 也重建推断边**：JOINABLE 依赖 `top_values/sample_values` 的 overlap 率；DISTRIBUTION_SIMILAR 依赖 `min/max/mean` 和 `histogram`；SEMANTIC_SYNONYM 依赖 `semantic_type`（间接来自 profile）；CORRELATED 依赖 `dtype` 和 joinable 边。数据源变化后这些统计值可能不同，推断边应随之更新。

结构边（CONTAINS、FOREIGN_KEY）和概念关联边（REPRESENTS、HAS_CONCEPT）不受影响。

### 2b.5 source_type 恢复

`TableNode` 存有 `source_type` 字段（"csv"、"parquet"、"database"）。重建时直接使用该字段构造 `DatasourceConfig`。如果 `source_type` 为空（旧版本数据），则从 `source` 字符串自动推断（数据库连接串 → database，.parquet → parquet，其余 → csv）。

### 2.2 边合并与过滤

```python
all_edges = structural_edges + inferred_edges + semantic_edges

filtered_edges = [
    e for e in all_edges
    if e.confidence >= config.confidence_threshold  # 默认 0.3
]
```

显式边 confidence=1.0，始终保留。低置信度推断边被丢弃。

### 2.3 CorrelationInferrer — 一次性传入全部 datasource

`CorrelationInferrer` 需要**跨表的 sample_data**做 merge + Pearson 计算，
所以将 `all_ds_infos` 一次性传入，由其内部自行提取所有表的采样数据：

```python
correlated_edges = correlation_inferrer.infer(
    all_columns, all_profiles, joinable_edges, all_ds_infos
)
```

内部逻辑：遍历 `all_ds_infos` 构建 `sample_dfs: dict[table_name, DataFrame]`，
再对每个 joinable edge 做 `pd.merge` + Pearson 计算。

**`add_table` 中不调用 CorrelationInferrer**——因为已有表的 sample_data 未持久化，
无法获取旧表的采样数据来做跨表 merge。

### 2.4 返回值

```python
{
    "status": "success",
    "datasources": len(datasource_configs),
    "stats": storage.get_graph_stats(),
}
```

---

## 3. add_datasource / add_table — 增量添加（含去重）

### 3.0 去重校验

`add_datasource` 在实际添加前会检查图谱中已存在的表（通过 table ID 匹配）。
ID 格式为 `table:{source}:{table_name}`，同一 source + 同一表名 → ID 相同 → 视为重复。

已存在的表会被**跳过**，只添加新表。返回结果中新增字段：

```python
{
    "status": "success",       # 有新表添加
    "added_tables": ["orders", "transactions"],  # 实际添加的表
    "skipped_tables": ["users"],                  # 已存在、跳过的表
    "stats": {...},
}

# 或者全部跳过时：
{
    "status": "skipped",
    "added_tables": [],
    "skipped_tables": ["users", "orders", "transactions"],
    "message": "All tables already exist in the graph. Use remove_table first if you want to re-add.",
    "stats": {...},
}
```

如果想重新添加已有表，需先 `remove_table` 再 `add_datasource`。

### 3.1 add_datasource 与 init_build 的差异

| 步骤 | 行为 |
|------|------|
| Connect | 单 datasource，可选过滤 `ds_info.tables` 仅保留指定表（table_names=None 则保留全部） |
| Extract/Profile | 仅新表 |
| Infer | **合并**已有列 + 新列、已有 Profile + 新 Profile 后推断 |
| Infer 输出 | 只保留 `involves_new_column(edge)` 的边 |
| Correlation | **不调用** |
| Store | **不清库**，`add_nodes_batch` / `add_edges_batch` 追加 |

### 3.2 已有图数据加载

```python
existing_columns = storage.get_nodes_by_type(NodeType.COLUMN)
existing_profiles = [
    storage.get_profile_for_column(col.id)
    for col in existing_columns
    if profile exists
]
all_columns_combined = existing + new
all_profiles_combined = existing_profiles + new_profiles
```

Joinable/Synonym/Distribution 在**合并集合**上 O(C²) 重算，再过滤出新边。

### 3.3 过滤新边

```python
new_column_ids = {c.id for c in new_columns}

def involves_new_column(edge):
    return edge.source_id in new_column_ids or edge.target_id in new_column_ids
```

避免重复处理纯旧列对（边 ID 相同会 OR REPLACE 覆盖，但浪费计算）。

---

## 4. remove_table — 删除表

```python
removed_column_ids = storage.remove_table(table_id)
orphan_count = storage.cleanup_orphaned_semantic_nodes()  # if cleanup_orphans
return {
    "status": "success",
    "removed_columns": len(removed_column_ids),
    "removed_orphans": orphan_count,
    "stats": get_graph_stats(),
}
```

**不**重新计算剩余边的 confidence，**不**删除仍被其他列引用的 Concept（除非 orphan cleanup 触发）。

---

## 5. 配置项在 Pipeline 中的绑定

| 配置字段 | 使用位置 |
|----------|----------|
| `graph_db_path` | `GraphStorage(config.graph_db_path)` |
| `joinable_overlap_threshold` | `JoinableInferrer(...)` |
| `correlation_threshold` | `CorrelationInferrer(...)` |
| `confidence_threshold` | 最终 `filtered_edges` |
| `llm` | `LLMMapper(config.llm)` — LLM 调用（timeout 控制单次请求超时） |
| `embedding` | `EmbeddingService(config.embedding, config.llm)` — 向量构建 + 混合检索 |
| `mapping_batch_size` | `LLMMapper._batch_size` — 每批列数（默认 15，减小可降低推理时间） |
| `embedding` | `EmbeddingService(config.embedding, config.llm)` — 向量构建 + 混合检索 |
| `sample_size` | 在 `DatasourceConfig` 传入 Connector（CLI build 时未显式设置，用 DatasourceConfig 默认 1000） |
| `mcp_tools` | MCP Server 辅助工具注册（`_get_tool_allowlist()`），Pipeline 不使用 |

**注意：** 所有 CLI/MCP 写命令（`add-table`、`rebuild`、`remove-table`）均通过 `DataLinkConfig.load()` 读取配置，`datalink_config.json` 中的阈值和 LLM 设置都会生效。

---

## 6. 完整数据流（全量）

```
DatasourceConfig[]
       │
       ▼
[DatasourceInfo[]]  ← Connector × N
       │
       ├──────────────────────────┐
       ▼                          ▼
TableNode[], ColumnNode[]    ColumnProfile[]
Edge[CONTAINS|FK]                 │
       │                          │
       └──────────┬───────────────┘
                  ▼
         Edge[JOINABLE|SYNONYM|DIST|CORR]
                  │
                  ▼
    ConceptNode[], EntityNode[], Edge[REPRESENTS|HAS_CONCEPT]
                  │
                  ▼
         SQLite (clear + batch insert)
                  │
                  ▼
    EmbeddingService.compute_embeddings() — 可选（embedding 配置时）
                  │
                  ▼
         node_embeddings (float32 BLOB + searchable_text)
                  │
                  ▼
            get_graph_stats()
```

---

## 7. 资源生命周期

```python
pipeline = BuildPipeline(config)
try:
    pipeline.rebuild()           # 重建（默认 mode=full）
    pipeline.rebuild(mode="vec")     # 只重建向量
    pipeline.rebuild(mode="profile") # 只重算统计 + 推断边
    pipeline.add_datasource(...) # 增量添加数据源（table_names=None 则加全部）
    pipeline.add_table(...)      # 增量添加单表（便捷入口）
finally:
    pipeline.close()  # storage.close()
```

CLI/MCP 调用映射：
- `datalink add-table --source X`（不传 --table） → `pipeline.add_datasource(config)`
- `datalink add-table --source X --table T` → `pipeline.add_table(config, T)`
- `datalink rebuild` → `pipeline.rebuild(mode="full")`
- `datalink rebuild --mode vec` → `pipeline.rebuild(mode="vec")`
- `datalink rebuild --mode profile` → `pipeline.rebuild(mode="profile")`

MCP `add_table` / `rebuild` / `remove_table` 在成功后 reset 全局 `_storage` / `_retrieval` 单例，强制下次查询重新打开 DB。

---

## 8. 错误处理

| 阶段 | 失败行为 |
|------|----------|
| 不支持的 datasource type | `ValueError` 向上抛 |
| add_table 表不存在 | `ValueError("Table 'x' not found")` |
| Connector 路径不存在 | `ValueError` |
| LLM 失败 | 静默跳过概念映射，不中断 build |
| Storage | SQLite 异常向上抛 |

CLI 捕获 Exception 打印后 `typer.Exit(1)`。

---

## 9. 扩展 Pipeline 的检查清单

新增处理阶段时建议：

1. 在 `init_build` / `rebuild` Step 4/5 之间或之后插入调用
2. `add_datasource` 中同步实现增量逻辑（是否合并已有节点）
3. 新边类型加入 `EdgeType` 枚举
4. 确认新边受 `confidence_threshold` 正确过滤
5. 更新 `get_graph_stats` / CLI info 展示（EdgeType 已自动枚举）
6. 补充 `tests/test_builder.py`

---

## 10. 性能特征（粗略）

设 D 个数据源、T 表、C 列、sample_size = S：

| 阶段 | 复杂度 |
|------|--------|
| Connect + 采样 | O(D × T × S) I/O |
| Extract | O(C) |
| Profile | O(C × S) |
| Joinable/Synonym/Dist | O(C²) 比较 |
| LLM Map | O(1) API 调用（列数影响 token） |
| Storage batch | O(C + E) 写入 |

多表、多列场景下 **Inferrer 平方级** 为主要 CPU 瓶颈。
