# Mapper 模块实现文档

**源码路径：** `src/datalink/mapper/llm_mapper.py`  
**输入：** `list[ColumnNode]` + `list[ColumnProfile]`（所有列，包括有 comment 的）  
**输出：** `(list[ConceptNode], list[EntityNode], list[Edge])`（D 类跨层边）  
**副输出：** `dict[str, str]`（table_id → inferred_comment，仅对缺少 comment 的表）

Mapper 有两个职责：
1. 将结构层 Column 映射到概念层 Concept / Entity。**所有列统一走 LLM 推理**——有 comment 的列会将 comment 作为额外信号带入 prompt。
2. 为缺少 SQL metadata comment 的表生成 LLM 推理描述（`generate_table_comments`）。

`add_table` 时新增的 Concept/Entity 需要与图中已有节点做消歧合并（`merge_with_existing`）。

---

## 1. LLMMapper

**文件：** `llm_mapper.py`

### 1.1 整体流程

```
map_columns(columns, profiles)
    │
    ├─ ≤15 列 → _map_columns_single()
    │     ├─ _build_columns_data()  → JSON Lines 文本（含 comment 字段）
    │     ├─ MAPPING_PROMPT_TEMPLATE.format(columns_data=...)
    │     ├─ _call_llm(prompt)
    │     ├─ _parse_response() → _try_parse_json() 渐进修复
    │     └─ _build_nodes_and_edges()
    │
    ├─ >15 列 → 分批映射（每批 ≤15 列）
    │     ├─ Batch 1 → _map_columns_single()
    │     ├─ Batch 2 → _map_columns_single() → merge_with_existing(batch2, accumulated1)
    │     ├─ Batch N → ... → merge_with_existing(batchN, accumulated_all)
    │     └─ 各批独立成功/失败，渐进合并去重
    │
    └─ 返回合并后的 (concepts, entities, edges)

add_table 时额外做一次 merge_with_existing 与图中已有节点消歧
```

### 1.2 Prompt 构造（`_build_columns_data`）

每列一行 JSON，字段：

```json
{
  "column_id": "column:...",
  "column_name": "...",
  "table_id": "table:...",
  "dtype": "integer",
  "semantic_type": "identifier",
  "null_rate": 0.0,
  "cardinality": 1000,
  "unique_rate": 1.0,
  "min_value": 1,          // 可选
  "max_value": 1000,       // 可选
  "mean_value": 500.5,     // 可选
  "sample_values": ["..."], // 最多 5 个
  "comment": "..."         // ← 关键：有 comment 的列也走 LLM，comment 作为额外信号
}
```

**所有列**都出现在 prompt 中。有 comment 的列在 JSON 中包含 `"comment"` 字段，供 LLM 作为额外信号使用。

### 1.3 LLM 调用参数

使用 OpenAI SDK 的 Chat Completions API（支持任何 OpenAI 协议的服务）：

```python
from openai import OpenAI

client = OpenAI(api_key=api_key, base_url=config.base_url)
client.chat.completions.create(
    model=config.model,
    messages=[
        {"role": "system", "content": "You are a data semantic analyzer. Always respond with valid JSON."},
        {"role": "user", "content": prompt},
    ],
    temperature=config.temperature,  # 默认 0.1
    max_tokens=config.max_tokens,    # 默认 16384
)
```

`_call_llm` 也支持 `temperature` 参数覆盖，用于 merge 判断时使用更低温度（`merge_llm_temperature`，默认 0.0）。

### 1.4 期望的 LLM 输出结构

```json
{
  "concepts": [{
    "name": "person_identifier",
    "description": "...",
    "unit": "",
    "dimension": "identity",
    "columns": ["column:id1", "column:id2"],
    "confidence": 0.9
  }],
  "entities": [{
    "name": "customer",
    "description": "...",
    "concept_names": ["person_identifier", "email_address"],
    "confidence": 0.85
  }]
}
```

Prompt 规则要求：多列可映射同一 Concept；Concept 名应泛化；Entity 聚合多个 Concept。

### 1.5 响应解析（`_parse_response` → `_try_parse_json`）

解析使用渐进式修复策略，对弱模型产生的格式错误 JSON 逐步尝试修复：

1. **直接解析**：提取 `{`...`}` 子串，`json.loads()`
2. **Markdown 代码块提取**：检测 `` ```json ... ``` `` 包裹
3. **移除注释**：删除 JS-style 注释（`// ...` 和 `/* ... */`），保留后续结构字符（`}` `]`）
4. **移除尾逗号**：删除 `},` 和 `],` 中的多余逗号
5. **括号闭合修复**：统计未闭合的 `{` `[`，补上缺失的 `}` `]`

每步修复后重新尝试 `json.loads`。所有步骤失败才返回 None。

校验必须含 `concepts` 和 `entities` 键。

### 1.6 图结构物化（`_build_nodes_and_edges`）

**Concept：**

```python
ConceptNode(
    id=f"concept:{concept_data['name']}",   # 按 name 全局唯一
    name=concept_data["name"],
    description, unit, dimension from JSON,
    properties={"source": "llm_inference", "confidence": ...},
)
```

对每个 `columns[]` 中的 col_id：

```python
Edge(REPRESENTS, col_id → concept.id, confidence=concept.confidence)
```

**Entity：**

```python
EntityNode(
    id=f"entity:{entity_data['name']}",
    ...
)
```

对每个 `concept_names[]`：

```python
Edge(HAS_CONCEPT, entity.id → f"concept:{concept_name}", confidence=...)
```

**注意：** `has_concept` 边的 target Concept **必须**已在同次 LLM 响应的 concepts 列表中定义，否则边指向不存在的节点。

---

## 2. Concept/Entity 消歧合并（`merge_with_existing`）

`add_table` 时，LLM 新产出的 Concept/Entity 可能与图中已有节点含义相同但名称不同。合并步骤确保含义一致的节点不会重复入库。

### 2.1 两阶段模型推理

合并算法采用 **Embedding 粗筛 + LLM 精判** 的两阶段模型推理：

| 阶段 | 有 Embedding 配置 | 无 Embedding 配置 |
|------|-------------------|-------------------|
| **阶段 A：Embedding 粗筛** | 计算新旧节点的 cosine 相似度，≥ `similarity_threshold` 的配对进入候选列表 | 跳过，候选列表为空 |
| **阶段 B：LLM 精判** | 候选配对 + 仅相关已有节点（出现在候选对中的）发给 LLM 确认合并 | 全量已有节点发给 LLM 判断所有合并 |
| **失败回退** | LLM 失败 → 不合并，新节点直接保留 | 同上 |

#### 阶段 A：Embedding 粗筛（`_embedding_prefilter`）

当 `embedding.model` 配置了模型名时（如 `text-embedding-3-small`）：

1. 对每个 Concept/Entity，用 `name | description | unit | dimension` 组合文本计算 embedding 向量
2. 计算每个新节点 vs 每个已有节点的 cosine 相似度
3. 相似度 ≥ `embedding.similarity_threshold`（默认 0.75）的配对进入候选列表
4. 候选列表传给 LLM 精判阶段作为参考

当 `embedding.model` 为空或 API 调用失败时，直接跳过粗筛，候选列表为空，LLM 精判阶段会直接对全量节点做合并判断。

#### 阶段 B：LLM 精判（`_llm_merge_judge`）

无论有没有 Embedding 粗筛，都走 LLM 精判：

1. 构建 merge prompt，包含：
   - 新 Concept/Entity 列表（id + name + description + unit + dimension）
   - 已有 Concept/Entity 列表：
     - **有 Embedding 候选对时**：仅注入出现在候选对中的已有节点（详细信息），其余不相似的节点完全省略，prompt 中注明只展示了部分已有节点
     - **无 Embedding 候选对时**：全量注入所有已有节点（兜底保证正确性）
   - 候选合并配对（来自 Embedding 粗筛，或"无候选——直接判断所有配对"）
2. LLM 返回合并方案：`{"merges": [...], "new_kept": [...]}`
3. 每个 merge 条目包含：`new_id`、`existing_id`、`reason`、`confidence`
4. confidence < `confidence_threshold` 的合并被自动过滤掉
5. LLM 判断考虑含义等价性，而非仅依赖名称相似度

Prompt 模板位于 `mapper/prompts/merge_prompt.txt`。

### 2.2 合并操作（`_execute_merge_plan`）

根据 LLM 返回的合并方案执行实际合并：

**概念合并：** 新概念 A 合并入已有概念 B

- 保留 B（ID 不变），丢弃 A（不入库）
- B 的 description 取两者中**更长者**（信息量更大）
- B 的 properties 合并（追加 A 的 source 信息等）

**实体合并：** 同理。

### 2.3 边重定向

合并节点后，所有引用被合并节点的边需要重定向：

| 边类型 | 重定向规则 | 示例 |
|--------|------------|------|
| `REPRESENTS` (Column → Concept) | `target_id` 从 A 改为 B | `column:... → concept:customer_id` 改为 `→ concept:person_identifier` |
| `HAS_CONCEPT` (Entity → Concept) | `source_id` 和 `target_id` 都可能需重定向 | `entity:user → concept:customer_id` 改为 `entity:customer → concept:person_identifier` |

### 2.4 边去重

重定向后可能产生 **(source_id, target_id, type)** 三元组完全相同的重复边。去重规则：**每组重复边保留 confidence 最高的一条，丢弃其余**。

### 2.5 Concept 属于多个 Entity — 不合并

一个 Concept 可以被多个 Entity 的 `has_concept` 边指向，这是多对多关系。只要 (source_id, target_id, type) 三元组不完全相同，边就共存，不做合并。

例如：

```
entity:customer → concept:person_identifier   (边E1)
entity:order   → concept:person_identifier   (边E2) ← 不同 source_id，不合并
```

但如果合并导致同一个三元组出现两次：

```
entity:customer → concept:person_identifier   (边E1, confidence=0.9)
entity:customer → concept:person_identifier   (边E3, confidence=0.85) ← 合并后重复
```

去重后保留 E1（confidence 更高）。

### 2.6 调用时机

| 场景 | 是否调用 merge_with_existing |
|------|------|
| `build_full`（图空）单批（≤15 列） | **不调用**——没有已有节点，直接返回 |
| `build_full`（图空）多批（>15 列） | **调用**——每隔 N 个 batch 合并一次（N = `merge_batch_interval`），最后一次必定合并 |
| `add_table`（图已有数据） | **调用**——与已有图节点消歧合并 |

分批映射的合并流程：不再每个 batch 都 merge，而是**累积 N 个 batch 后做一次 merge**（N 由 `merge_batch_interval` 控制，默认 10）。这样可以大幅减少 merge LLM 调用次数。最后一个 batch 后必定触发 merge，确保所有累积节点都被处理。

例如（`merge_batch_interval=2`，6 个 batch）：

```
Batch 1 → 累积: [person_id]           → pending
Batch 2 → 累积: [person_identifier]    → merge(interval=2) → person_identifier ≡ person_id → accumulated=[person_id]
Batch 3 → 累积: [email]               → pending
Batch 4 → 累积: [email_address]        → merge(interval=2) → email_address ≡ email → accumulated=[person_id, email]
Batch 5 → 累积: [order_id]            → pending
Batch 6 → 累积: [order_identifier]     → merge(最后一个batch) → order_identifier ≡ order_id → accumulated=[person_id, email, order_id]
```

当 `merge_batch_interval=1`（默认旧行为）时，每个 batch 都触发 merge。

### 2.7 Embedding 配置

在 `datalink_config.json` 中配置：

```json
{
  "embedding": {
    "model": "text-embedding-3-small",
    "api_key": "",
    "base_url": "",
    "similarity_threshold": 0.75
  },
  "merge_llm_temperature": 0.0
}
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `embedding.model` | `""` | 模型名，空=跳过 Embedding 粗筛，纯 LLM 判断 |
| `embedding.api_key` | `""` | 空=回退到 `llm.api_key` |
| `embedding.base_url` | `""` | 空=回退到 `llm.base_url` |
| `embedding.similarity_threshold` | `0.75` | cosine 相似度阈值，低于此的配对不会成为候选 |
| `merge_llm_temperature` | `0.0` | merge LLM 调用温度，低温度确保判断确定性 |
| `merge_batch_interval` | `10` | 分批推理时每 N 个 batch 才做一次 merge（1=每个 batch 都 merge） |

---

## 3. Pipeline 中的调用

```python
# Step 5: Concept mapping — all columns via LLM
llm_mapper = LLMMapper(self.config)
all_concepts, all_entities, all_semantic_edges = llm_mapper.map_columns(
    all_columns, all_profiles
)

# add_table 时额外做合并
if existing_concepts or existing_entities:
    all_concepts, all_entities, all_semantic_edges = llm_mapper.merge_with_existing(
        all_concepts, all_entities, all_semantic_edges,
        existing_concepts, existing_entities,
    )
```

与结构边、推断边一并写入 Storage，仍受 `confidence_threshold` 过滤。

---

## 4. 数据流图

```
ColumnNode + ColumnProfile (all columns, including those with comments)
        │
        ▼
    LLMMapper.map_columns()
        │
        ├─ ≤15 列 → _map_columns_single → 直接返回
        │
        └─ >15 列 → 分批映射 + 渐进合并
              │
              Batch 1 ─► _map_columns_single ─► concepts/entities/edges
              Batch 2 ─► _map_columns_single ─► merge_with_existing(batch2, batch1_accum)
              Batch 3 ─► _map_columns_single ─► merge_with_existing(batch3, batch1+2_accum)
              ...
              │
              ▼
        返回合并后的 (concepts, entities, edges)

    add_table → merge_with_existing(新结果, 已有图节点)
            │
            ├─ _embedding_prefilter (可选) → 候选合并配对
            ├─ _llm_merge_judge → 合并方案
            ├─ _execute_merge_plan → 消歧合并 + 重定向边
            └─ _deduplicate_edges → 去重
            │
            ▼
        GraphStorage.add_nodes_batch(concepts + entities)
        GraphStorage.add_edges_batch(semantic_edges)
```

---

## 5. 失败模式

| 情况 | 行为 |
|------|------|
| 无 API Key | LLMMapper 返回空 → 整个概念层为空 |
| LLM 超时/异常 | 记录 error，返回空 |
| JSON 解析失败（单批） | 渐进修复 5 步尝试 → 仍失败则该批次丢弃，其他批次不受影响 |
| JSON 解析失败（所有批次） | 所有概念层为空（只有全部批次失败时才空） |
| Entity 引用未知 concept_name | 产生悬空 has_concept 边 |
| 合并后边重复 | 自动去重，保留 confidence 最高 |
| 跨批次概念/实体重复 | `merge_with_existing` 自动合并，保留先出现的节点 |
| Embedding API 调用失败 | 跳过粗筛，直接走纯 LLM 判断 |
| Embedding 未配置 | 同上——纯 LLM 判断 |
| merge LLM 调用失败 | 不合并，所有新节点直接保留 |

---

## 6. 表描述生成（`generate_table_comments`）

当表没有 SQL metadata comment 时，`generate_table_comments` 用 LLM 推理出一句描述。

### 6.1 调用时机

Pipeline Step 5（概念映射）完成后，Step 6（入库）之前。对 init_build 和 add_table 都会调用。

### 6.2 输入信号

Prompt 向 LLM 提供每张表的信息：
- 表名 + 行数
- 每列的 name、dtype、semantic_type、null_rate、cardinality
- 每列的 top_values（频率最高的值）
- 每列关联的 concept/entity 名称（如果 Step 5 已推理出来）

### 6.3 输出格式

LLM 返回 JSON：`{"tables": [{"table_id": "...", "comment": "..."}]}`

每条 comment 限制为一句话（≤200 字符），自动写入 TableNode 的 `properties["comment"]`。

### 6.4 跳过条件

- 表已有 SQL metadata comment → 不调用 LLM
- 无 API Key → 整批跳过
- LLM 返回空或解析失败 → 跳过，表无 comment

### 6.5 Prompt 模板

位于 `mapper/prompts/table_comment_prompt.txt`，可独立编辑调整。

---

## 7. 调优建议

- 降低 `llm.temperature`（已默认 0.1）提高映射稳定性
- 数据库源尽量填写 column comment，LLM 据此做更准确的映射
- 列数过多时自动分批（每批 15 列），各批独立成功/失败，跨批次合并去重——修改 `_BATCH_SIZE` 可调整批次大小
- **merge 频率控制**：`merge_batch_interval`（默认 10）决定每隔多少个 batch 才做一次合并。6 个 batch + interval=10 → 只在最后一个 batch 做 1 次 merge（省 5 次 LLM 调用）；interval=1 → 旧行为，每个 batch 都 merge。大部分数据源的 batch 数 ≤ 10，设置 interval=10 即可确保只在最后做一次 merge
- merge 合并判断使用 `merge_llm_temperature`（默认 0.0），确保判断确定性
- 配置 embedding 模型可大幅减少 merge LLM 调用的 token 消耗——粗筛过滤掉不相关的配对，LLM 只需确认少量候选
- embedding 粗筛阈值 `embedding.similarity_threshold`（默认 0.75）可根据实际效果微调：降低阈值增加候选对（更保守、更少遗漏），提高阈值减少候选对（更激进、LLM 负担更轻）
- 弱模型 JSON 格式问题：`_try_parse_json` 自动修复尾逗号、JS 注释、括号未闭合等常见错误，5 步修复后仍失败则该批次跳过
- Prompt 模板位于 `mapper/prompts/` 目录下（`mapping_prompt.txt`、`merge_prompt.txt`、`table_comment_prompt.txt`），均可独立编辑调整
