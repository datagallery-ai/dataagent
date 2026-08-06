# Storage 与 Retrieval 模块实现文档

**源码路径：** `src/datalink/graph/`  
**Storage 输入：** 节点、边、Profile 的批量写入  
**Retrieval 输入：** 查询参数 → JSON 可序列化结果或格式化文本

---

# Part A: GraphStorage

**文件：** `storage.py` + `schema.sql`

## A.1 数据库初始化

```python
db_path = _resolve_db_path(db_path)  # 裸文件名 → ~/.datalink/storage/xxx.db
sqlite3.connect(db_path)
PRAGMA foreign_keys = ON
PRAGMA journal_mode = WAL
executescript(schema.sql)
```

**路径解析规则：**

| 配置值 | 实际路径 |
|--------|----------|
| `"datalink.db"`（裸文件名） | `~/.datalink/storage/datalink.db` |
| `"project_a/main.db"`（相对路径） | `~/.datalink/storage/project_a/main.db` |
| `"/data/graph.db"`（绝对路径） | `/data/graph.db`（原样使用） |

`~/.datalink/storage/` 目录在首次运行时自动创建。

| `remove_edges_by_types` | 批量删除指定类型的边（profile rebuild 时清理旧的推断边） |

## A.2 表结构

| 表 | 主键 | 核心列 |
|----|------|--------|
| `nodes` | `id TEXT` | `type`, `name`, `properties JSON` |
| `edges` | `id TEXT` | `source_id`, `target_id`, `type`, `confidence`, `properties JSON` |
| `column_profiles` | `id TEXT` | `column_id FK→nodes`, `properties JSON` |
| `metadata` | `key TEXT` | `value`, `updated_at` |

**外键级联：** 删 node → 自动删关联 edges、profiles。

### pending_edges 表

| 列 | 说明 |
|----|------|
| `id TEXT PK` | 边唯一标识 |
| `source_id TEXT` | 源节点 ID（**无 FK 约束**，允许引用不存在的节点） |
| `target_id TEXT` | 目标节点 ID（**无 FK 约束**，允许引用不存在的节点） |
| `type TEXT` | 边类型（同 EdgeType 枚举值） |
| `confidence REAL` | 置信度 |
| `properties TEXT` | JSON blob |
| `missing_endpoints TEXT` | JSON 数组：`["source"]` / `["target"]` / `["source","target"]`，标记哪端节点缺失 |
| `created_at TIMESTAMP` | 创建时间 |

**无 FK 约束**是关键设计——`source_id` / `target_id` 是纯 TEXT，允许引用尚未入库的节点 ID。

## A.3 节点序列化策略

写入时**仅**存四元组 `(id, type, name, properties)`；Column/Table 的 typed 字段（`dtype`, `table_id` 等）由 Pipeline 预先写入 `properties` JSON。

读取时 `_row_to_node()` 按 `type` 反序列化为对应 Pydantic 子类：

```python
NodeType.COLUMN  → ColumnNode(table_id=props["table_id"], ...)
NodeType.TABLE   → TableNode(source=props["source"], column_ids=props["column_ids"], ...)
NodeType.CONCEPT → ConceptNode(description=props["description"], ...)
NodeType.ENTITY  → EntityNode(...)
```

## A.4 Profile 序列化

```python
props = profile.model_dump(exclude={"id", "column_id"}, mode="json")
INSERT INTO column_profiles (id, column_id, properties) VALUES (...)
```

读取：`ColumnProfile(id=..., column_id=..., **props)`。

## A.5 批量写入

Pipeline 使用：

- `add_nodes_batch(nodes)` — `executemany` + 单次 commit
- `add_edges_batch(edges)`
- `add_profiles_batch(profiles)`

策略：`INSERT OR REPLACE`，同 ID 覆盖。

## A.6 Pending Edge 操作

| 方法 | 说明 |
|------|------|
| `add_pending_edge(edge)` | 写入单条 pending edge |
| `add_pending_edges_batch(edges)` | 批量写入（executemany + 单次 commit） |
| `get_pending_edge(id)` | 查单条 |
| `get_pending_edges_for_node(node_id)` | 查涉及某节点的所有 pending edges |
| `get_all_pending_edges()` | 查全部 |
| `resolve_pending_edges(available_node_ids)` | 核心：扫描 pending_edges，将两端均在 `available_node_ids` 中的边移入 edges 表，删除原 pending 记录 |
| `remove_pending_edge(id)` | 删单条 |
| `cleanup_pending_edges_for_removed_nodes(removed_ids)` | 删除引用已移除节点的 pending edges（永远无法 resolve） |
| `count_pending_edges(edge_type)` | 统计 |

**`resolve_pending_edges` 算法：**

```
1. SELECT * FROM pending_edges
2. 对每条：if source_id ∈ available_node_ids AND target_id ∈ available_node_ids:
   → INSERT OR REPLACE INTO edges (same columns)
   → DELETE FROM pending_edges WHERE id = ?
3. COMMIT; return resolved_count
```

**`cleanup_pending_edges_for_removed_nodes` 算法：**

对每个被移除的节点 ID，删除 `source_id = nid OR target_id = nid` 的所有 pending edges。这些边永远无法 resolve。

## A.7 remove_table 算法

```
1. contains_edges = get_edges_for_node(table_id, CONTAINS)
2. column_ids = [e.target_id for e in contains_edges]
3. all_ids = [table_id] + column_ids
4. 对每个 nid: DELETE edges WHERE source_id=nid OR target_id=nid
5. 对每个 col_id: DELETE column_profiles WHERE column_id=col_id
6. DELETE nodes WHERE id IN column_ids + table_id
7. return column_ids  → 供 Pipeline 决定是否 cleanup orphans
```

**不**自动删 Concept/Entity；由 `cleanup_orphaned_semantic_nodes()` 处理。

## A.8 cleanup_orphaned_semantic_nodes

两阶段清理，从概念层逐层向外剥离孤立节点：

**Stage 1 — Concept 清理：**

```python
anchored_concept_ids = {所有有 represents 边从 Column/Table 指向的 concept ID}
for concept in all_concepts:
    if concept not in anchored_concept_ids:
        DELETE concept 的所有边 + concept 节点
```

只有被结构层节点（Column/Table）通过 `represents` 边锚定的 Concept 才保留。`has_concept` 入边（来自 Entity）不算锚定——它是概念层内部连接，结构层删了之后没有意义。

**Stage 2 — Entity 清理（此时孤立 Concept 已被删除）：**

```python
for entity in all_entities:
    outgoing_to_structural = COUNT edges WHERE source=entity AND target IN (Column/Table)
    has_concept_to_anchored = COUNT has_concept 边 WHERE target IN anchored_concept_ids
    if both == 0:
        DELETE entity 的所有边 + entity 节点
```

Entity 通过 `has_concept` 边间接锚定：如果它的 `has_concept` 目标 Concept 在 Stage 1 中存活（被结构层 `represents` 锚定），则 Entity 也保留。

## A.9 get_graph_stats

遍历所有 `NodeType` / `EdgeType` 枚举值分别 `COUNT(*)`，返回汇总 dict（含 0 计数类型）。另外包含 `pending_edge_count` 和 `pending_edge_type_counts`。

---

# Part B: GraphRetrieval

**文件：** `retrieval.py`

## B.1 search_nodes

**主路径：** `storage.search_nodes_by_name(query, node_type, limit)`

```sql
SELECT ... FROM nodes WHERE name LIKE '%{query}%' [AND type=?] LIMIT ?
```

**扩展路径（Column）：** 额外 SQL：

```sql
SELECT ... FROM nodes
WHERE type='column' AND properties LIKE '%{query}%' LIMIT ?
```

对 JSON 做子串匹配，再 `json.loads` 检查 `semantic_type` 是否含 query（小写）。去重后截断至 `limit`。

**返回结构：** 每节点含 `id`, `type`, `name`, `properties`, `edge_count`, `edges_summary`（最多 5 条邻接边摘要）。

## B.2 get_node

加载节点 + 全部邻接边；每条边附带 `direction`（`"outgoing"` 或 `"incoming"`，表示对当前节点的方向）和 `other_node`（对端 id/name/type）。

- `direction="outgoing"`：当前节点是边的 source（出边）
- `direction="incoming"`：当前节点是边的 target（入边）

CLI 显示时出边用 `→`，入边用 `←` 标注方向，并显示对方节点名称而非 target_id。

Column 类型额外挂载 `profile` 子对象（dtype、semantic_type、null_rate、cardinality、sample_values 等）。

**`suggested_edges` 字段：** 返回涉及此节点的所有 pending edges（两端节点缺失的边），每条附带 `missing_endpoints` 和 `note` 人类可读提示。这些边不参与路径发现和子图扩展，仅作为"建议关系"展示。

## B.3 find_paths — 路径发现

### 目标

在 `max_depth` 跳内找从 `source_id` 到 `target_id` 的路径，按**路径置信度积**降序，最多 10 条。

路径置信度：`∏ edge.confidence`（各边 confidence 连乘）。

### 主算法：SQLite 递归 CTE

```sql
WITH RECURSIVE paths AS (
    -- 基：起点，空边路径，depth=0，confidence=1.0
    SELECT ?, '[]', 0, 1.0, ?

    UNION ALL

    -- 递归：沿边扩展
    SELECT e.target_id,
           json_array_append(p.path_edges, e.id),
           p.depth + 1,
           p.confidence * e.confidence,
           p.source_id
    FROM paths p
    JOIN edges e ON (e.source_id = p.current_id OR e.target_id = p.current_id)
                  AND e.source_id != p.current_id
    WHERE p.depth < max_depth
      AND [edge type filter]
      AND cycle prevention (见下)
)
SELECT ... WHERE current_id = target_id ORDER BY confidence DESC LIMIT 10
```

**无向遍历：** 边可从 source 或 target 方向进入，下一跳取 `e.target_id`（实现上对反向边的处理较简化）。

**环检测局限：** CTE 中用 `json_each(path_edges)` 存的是 **edge id**，不是 node id，环检测**不完全可靠**。

### 降级：`_python_bfs_paths`

CTE 失败时启用 BFS 队列 `(current_node, edge_path, confidence)`：

- 扩展邻接边，过滤 `edge_types`
- 用 edge_path 中已有 source/target 集合防环
- 到达 target 且 path 非空则记录
- 按 confidence 排序取 top 10

Python 版环检测更准确，通常作为 fallback。

### 结果组装

将 path 中 edge id 序列解析为 `nodes[]` 和 `edges[]` 详情列表。

## B.4 extract_subgraph — 子图扩展

**层次 BFS（按 hop）：**

```
visited_nodes = seed node_ids
visited_edges = {}
current_layer = seed set

for hop in 1..max_hops:
    for nid in current_layer:
        for edge in get_edges_for_node(nid):
            if edge.id not in visited:
                visited_edges.add(edge.id)
                other = 对端节点
                if other not in visited_nodes:
                    visited_nodes.add(other)
                    next_layer.add(other)
    current_layer = next_layer
```

返回 `nodes[]`, `edges[]`, `stats{node_count, edge_count, hops}`。

**无** confidence 过滤、无 edge type 过滤。

## B.5 get_pending_edges — 悬空边查询

**参数：** `node_id`（可选）、`edge_type`（可选）、`limit`

**逻辑：**
1. `node_id` → `get_pending_edges_for_node(node_id)`，否则 `get_all_pending_edges()`
2. `edge_type` → Python 侧过滤
3. 截断至 `limit`

**返回结构：** 每条 pending edge 含 `id`, `type`, `source_id`, `target_id`, `confidence`, `missing_endpoints`, `note`, `properties`。

## B.6 list_datasets

对所有 `TableNode`：

```python
column_count = len(CONTAINS edges)
inferred_edge_count = 对每列统计非 CONTAINS/FK 的邻接边数之和
pending_fk_count = 对每列统计 pending_edges 中 type=foreign_key 的数量
```

返回 `id`, `name`, `source`, `row_count`, `column_count`, `inferred_edge_count`, `pending_fk_count`。

---

## C. Explore — 万能检索入口

**文件：** `retrieval.py` → `GraphRetrieval.explore()`

### C.1 设计理念

DataLink 暴露一个万能检索工具 `datalink_explore`：

- **单工具入口**：agent 给一个 query，一次调用获得完整上下文
- **多维度召回**：名称、semantic_type、concept/entity、comment/description 多路匹配 + 沿边扩展
- **实体自包含输出**：每个列节点自包含所有信息（业务含义 + 数据特征 + 关系 + 查询意义），不再按格式分段
- **概念层吸收**：Concept/Entity 不作为独立输出节点，它们的描述被吸收进对应列的 meaning 行。搜索阶段仍用 concept/entity 做索引扩展，但输出时只展示 table + column
- **自适应预算**：按 dataset 数量调整输出量
- **focus 调深**：通过 focus 参数切换输出重心，不需要换工具

辅助工具（search_nodes, get_node 等）保留为内部方法，explore 内部调用它们，不默认暴露给 agent。

### C.2 方法签名

```python
def explore(
    self,
    query: str,
    max_nodes: int | None = None,  # None → 自适应预算
    focus: str | None = None,       # "join_paths" | "schema" | "data_profile" | None
    mask_credential: bool = True,   # 是否遮蔽数据库凭证
) -> str:  # 返回格式化文本，不是 JSON dict
```

### C.3 内部流程

```
explore(query)
  ① _resolve_query(query, limit) → ResolvedNode 列表
  ② get_explore_budget(dataset_count, focus) → ExploreBudget
  ③ _build_context(resolved, budget) → NodeContext 列表
  ④ _build_relationship_map(node_ids, budget) → RelationshipMap
     （间接路径仅遍历 FK/joinable 边，仅保留跨表路径）
  ⑤ _format_output(contexts, rel_map, resolved, budget) → 文本输出
     过滤掉 concept/entity → 按表分组 → 每列自包含格式化 → 跨表查询模式段
```

### C.4 _resolve_query — 多维度匹配 + 沿边扩展

**匹配维度：**

| 维度 | 覆盖场景 | 实现 | 分数 |
|---|---|---|---|
| 名称精确 | `"orders"` → TableNode:orders | `name.lower() == token.lower()` | 1.0 |
| 名称子串 | `"customer"` → customer_id, customer_name | `LIKE '%token%'` | 0.8 |
| semantic_type | `"email"` → 所有 semantic_type=email 的列 | properties JSON 搜索 | 0.7 |
| comment/description | `"订单金额"` → comment 或 description 包含此文字的节点 | properties JSON 搜索 | 0.5 |

**沿边扩展（召回关键）：**

| 被匹配节点类型 | 扩展边类型 | 扩展目标 | 分数 |
|---|---|---|---|
| ConceptNode | `represents`（入边） | 所有 ColumnNode | 0.6 × edge.confidence |
| EntityNode | `has_concept`（出边） | 所有 ConceptNode | 0.5 × confidence |
| EntityNode → ConceptNode | `represents` | ColumnNode | 0.4 × 双边 confidence |
| TableNode | `contains` | 所有 ColumnNode | 0.4 |
| ColumnNode | `foreign_key/joinable/semantic_synonym` | 关联 ColumnNode | 0.3 × confidence |

合并、去重、按 relevance_score 排序，截断至 `budget.max_nodes`。

### C.5 ExploreBudget — 自适应输出预算

```python
@dataclass
class ExploreBudget:
    max_output_chars: int
    max_nodes: int
    max_edges_per_node: int
    max_edges_per_relationship_kind: int
    max_sample_values: int
    max_columns_per_table: int
    max_pending_edges_per_node: int
    include_relationships: bool
    include_additional_nodes: bool
    include_budget_note: bool
    include_completeness_signal: bool
    include_low_confidence_marker: bool
```

| datasets | max_output | max_nodes | max_edges | sample_vals | relationships? | additional? | budget_note? |
|---|---|---|---|---|---|---|---|
| < 3 | 8000 | 8 | 3 | 3 | False | False | False |
| < 10 | 12000 | 12 | 5 | 5 | True | False | True |
| < 50 | 16000 | 15 | 7 | 5 | True | True | True |
| ≥ 50 | 20000 | 20 | 10 | 5 | True | True | True |

**focus 参数调整：**

| focus | 效果 |
|---|---|
| `data_profile` | sample_values 翻倍，edges 减半 |
| `join_paths` | edges 翻倍，sample_values 减半，强制 include_relationships |
| `schema` | max_nodes 翻倍，sample_values=0（只给 dtype） |
| None | 均衡（默认） |

### C.6 _build_context — 节点上下文构建

对每个 ResolvedNode：
1. `storage.get_node()` → 基本信息
2. `storage.get_edges_for_node()` → 邻接边，按类型分组（join/semantic/statistical/contains），每组截断至 `budget.max_edges_per_node`
3. Column 类型 → `storage.get_profile_for_column()` → 关键指标摘录（dtype, semantic_type, null_rate, cardinality, sample_values）
4. `storage.get_pending_edges_for_node()` → 悬空边标注（cap 3 per node）

### C.7 _build_relationship_map — 关系网络

- **直接连接**：resolved 节点之间的边，按类型分组（join/semantic/statistical）
- **间接路径**：resolved 节点间 `find_paths` ≤3 hop，**仅遍历 FK/joinable 边**（不遍历 contains/represents 等结构性边），**仅保留跨表路径**（同一表内的路径无查询价值）

### C.8 _format_output — 输出格式

**核心原则：实体自包含 + 概念层吸收**

1. 过滤掉 concept/entity contexts — 概念信息通过 `_generate_meaning()` 吸收到列的 meaning 行
2. 按表分组（`_group_by_table`），每个表一个 `##` 段
3. 表级 meaning 行：如果表有 comment（来自 SQL metadata 或 LLM 推理），显示为一行
4. 每列自包含格式化（`_format_column_selfcontained`）
5. 跨表查询模式段（`_format_cross_table_paths`）

**表级输出格式：**

```
## schools — 2 columns, 17,686 rows, source: postgresql://...
- meaning: California schools with eligibility and participation data
```

表的 meaning 行只显示表的 comment。没有 comment 的表不显示 meaning 行。

**表 comment 的来源：**
- SQL metadata（数据库表注释） — 优先
- LLM 推理生成（`LLMMapper.generate_table_comments`） — 当 SQL metadata 缺失时的后备

**单列输出格式：**

```
### customer_id (integer, identifier)
- meaning: foreign key referencing customers.id — use `JOIN orders ON orders.customer_id = customers.id` to link orders with their customer
- data: null 2%, unique 95%, top values: [101, 205, 340]
- also related: synonym↔users.user_id (0.72) — same entity type
- pending foreign_key → ? (Referenced node not yet in graph)
```

**meaning 行的组装逻辑：**

从多个来源组合成一句完整的描述，用 `; ` 连接：
1. column comment（SQL metadata 或 CSV header）
2. concept/entity 吸收（represents 边 → "represents concept 'revenue' (total sales, unit: USD)"）
3. FK/joinable 翻译（→ 查询意义，如 "foreign key referencing customers.id — use JOIN ...")

**also related 行：**

meaning 行只包含 comment + concept吸收 + FK/joinable。其余关系（synonym, semantic_type_match, correlated, distribution_similar）紧凑列在 also related 行。

**data 行的值展示：**

使用 `top_values`（频率排序、天然不重复）代替随机 `sample_values`。避免高频率值重复出现。

**跨表查询模式段：**

```
## Cross-table query patterns
orders.customer_id → customers.id (FK, conf 1.00)
  SQL: SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id
```

只展示跨表的 FK/joinable 连接，附带 SQL JOIN 模板。间接多跳路径在此段展示。

**已删除的旧段落：**
- Node semantics 段（含义已内联到 meaning 行）
- Budget note 段（对 agent 无操作意义）
- Additional relevant 段（已内联到 also related）
- 独立 concept/entity 输出段（概念层吸收）

### C.9 辅助工具启用机制

**默认只暴露：** `datalink_explore` + `build_graph` + `add_table` + `remove_table`

**可选暴露（环境变量 `DATALINK_MCP_TOOLS`）：**

```bash
DATALINK_MCP_TOOLS=datalink_search_nodes,datalink_get_node,datalink_find_paths
```

工具名使用全名（不用缩写），避免歧义。

| 工具全名 | 能力 |
|---|---|
| `datalink_search_nodes` | 精确名称搜索（位置列表） |
| `datalink_get_node` | 单节点详情 + 全部邻接边 + 完整 profile |
| `datalink_find_paths` | 两节点间路径（可指定 edge_type） |
| `datalink_extract_subgraph` | 从指定节点按 hop 扩展子图 |
| `datalink_list_datasets` | 所有表及统计概览 |
| `datalink_list_pending_edges` | 悬空边列表 |

---

## D. Storage ↔ Retrieval 关系

```
BuildPipeline
    → GraphStorage (write)
CLI / MCP
    → GraphRetrieval.explore() (万能检索，内部调用 search/get/paths 等)
    → GraphRetrieval.search_nodes / get_node / find_paths 等 (辅助方法，供 explore 和 CLI 使用)
```

Retrieval **不**直接 SQL 复杂 analytics（除 find_paths CTE），大部分走 Storage 封装方法。

---

## E. 索引（schema.sql）

| 索引 | 用途 |
|------|------|
| `idx_nodes_type`, `idx_nodes_name` | 按类型/名称查 |
| `idx_edges_source/target`, `(source,type)`, `(target,type)` | 邻接边、路径遍历 |
| `idx_edges_confidence` | 按置信度过滤（预留） |
| `idx_profiles_column` | 列 → Profile |

---

## F. 已知限制

| 项 | 说明 |
|----|------|
| 搜索 | LIKE 子串匹配 + embedding 向量检索（混合检索）；embedding 未配置时退化为纯全文检索 |
| find_paths CTE | 环检测与无向边处理不完善 |
| 单 SQLite 文件 | 无分布式、无并发写优化 |
| properties JSON | 无法 SQL 内高效查询嵌套字段（除 LIKE） |
| pending edges | 永远无法 resolve 的 pending 边（目标数据源永不加入）仅作为 suggested_edges 展示，不参与遍历 |
