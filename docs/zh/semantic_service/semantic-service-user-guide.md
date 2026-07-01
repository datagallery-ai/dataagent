# Semantic Service 使用指南

Semantic Service 旨在把业务数据、数据表、字段、指标、本体图谱和业务动作整理成 Agent 可调用的语义上下文。它的目标不是替代模型推理，而是在模型推理前提供更可靠的业务语义、数据语义和查询边界，让 Agent 知道“有哪些数据可以用、字段是什么意思、对象之间有什么关系、哪些查询或动作是合法的”。当前开源版本聚焦 NL2SQL 所需的数据语义能力。

当前阶段，Semantic Service 主要提供面向 NL2SQL 的增强元数据 REST 能力，并优先围绕 GaussVector 做了语义层向量索引、召回排序和 schema 感知增强，用于承载表描述、字段描述、指标口径和业务关键词等元数据向量，支撑 NL2SQL 在生成 SQL 前完成候选 schema 召回与语义对齐；Ontology 本体服务相关能力正在开发中。

- **增强元数据 REST 能力**：当前可用，只用于 NL2SQL 能力，包括 NL2SQL Agent 单独运行，以及主 Agent 通过 `nl2sql_sub_agent_tool` 调用 NL2SQL 子 Agent。优先结合 GaussVector，Semantic Service 可将业务元数据沉淀为可召回、可排序、可复用的 schema 语义索引，使语义层具备更强的候选 schema 发现能力。
- **Ontology 本体服务**：开发中能力，目标是提供业务对象、关系、属性、路径、统计和服务端动作等本体/知识图谱语义。相关服务、Skill、CLI 和接入示例将在能力稳定后补充。

二者解决的问题不同：增强元数据 REST 能力让 NL2SQL 更准确地理解“数据在哪里、字段是什么意思、表之间如何关联”；规划中的 Ontology 本体服务会让 Agent 能够理解“业务对象是什么、对象之间有什么关系、有哪些属性、路径、统计或动作可以查询”。

## 1. 能力边界

| 组件 | 推荐入口 | 主要作用 |
| --- | --- | --- |
| 增强元数据 REST 能力 | NL2SQL Agent / `nl2sql_sub_agent_tool` | 为 NL2SQL 提供 schema、字段语义、表关联和语义召回能力 |
| Ontology 本体服务 | 开发中 | 计划提供本体/知识图谱 schema 发现、实体关系查询、路径查询、统计聚合和服务端 action 查询能力 |

增强元数据 REST 能力的作用是让 NL2SQL 在生成 SQL 前获得可靠的数据语义上下文：表、字段、字段描述、字段类型、示例值以及表关联关系。规划中的 Ontology 本体服务将提供业务知识图谱视角，让 Agent 在处理对象关系、业务规则、路径检索、统计聚合或服务端动作时，有结构化的本体依据，而不是猜测实体标签、关系名、属性名或 UUID。

## 2. 整体使用方式

Semantic Service 在 Agent 中可按当前可用能力和规划能力分为三种使用形态：

| 形态 | 适合场景 | 推荐入口 |
| --- | --- | --- |
| NL2SQL Agent 独立运行 | 用户问题本身就是自然语言转 SQL，Agent 只负责 schema 感知、SQL 生成、执行与结果返回 | `AGENT_CONFIG.type: "nl2sql"` + `SEMANTIC_LAYER` |
| 主 Agent 调 NL2SQL 子 Agent | 主 Agent 负责整体任务规划，只有遇到数据库查询时才委托 NL2SQL | `nl2sql_sub_agent_tool` + 主 Agent 的 `DATABASE` / `SEMANTIC_LAYER` |
| 本体/图谱查询 | 任务需要查询业务对象、关系、属性、路径、统计或服务端动作 | 开发中 |

一个复杂数据任务可以同时使用这些能力，但推荐保持职责清晰：

1. 主 Agent 负责理解用户目标、拆分任务和组织最终回答。
2. SQL 类问题交给 NL2SQL Agent 或 `nl2sql_sub_agent_tool`，由 Semantic Service 补全数据语义。
3. 本体/图谱类问题属于开发中的 Ontology 本体服务能力；在相关接入能力稳定前，建议由用户提供明确对象和约束后再进入后续查询。
4. 不让主 Agent 直接猜字段、表关系、本体标签、属性名或 UUID。

## 3. 增强元数据 REST 能力

增强元数据 REST 能力面向结构化数据，核心价值是把数据库中的“表和字段”转化为模型可理解、可校验的语义上下文。在 NL2SQL 流程中，它主要提供：

- 表级语义：表名、表描述、业务含义。
- 字段级语义：字段名、字段描述、字段类型、示例值。
- 关系语义：哪些表可以 join，以及 join key 是什么。
- GaussVector 语义索引增强：通过 GaussVector 承载表描述、字段描述、指标口径和业务关键词向量，提升候选 schema 召回质量，并增强 NL2SQL 感知阶段的语义匹配能力。

当前工程中，增强元数据 REST 能力不作为通用 ReAct 感知工具来介绍，只作为 NL2SQL 能力的一部分使用。

### 3.1 优先支持 GaussVector 的语义层增强

在语义层中，GaussVector 是优先支持的向量检索增强组件。Semantic Service 将表、字段、指标口径、业务描述等文本转成 embedding，并通过 GaussVector 保存和检索这些向量化语义资产。NL2SQL 在 schema 感知阶段基于用户问题和抽取关键词发起语义检索，召回候选表、候选字段和表描述，再与 join 关系、字段类型一起组成 SQL 生成前的上下文。

围绕 GaussVector 的增强让业务元数据不再只是静态说明文档，而是可以形成可检索、可排序、可复用的语义资产，为自然语言问数提供更稳定的候选 schema 召回，降低模型直接猜表、猜字段的概率。

### 3.2 在 NL2SQL Agent 中使用

当 `AGENT_CONFIG.type: "nl2sql"` 时，NL2SQL 内部的 Perceptor 会读取 `SEMANTIC_LAYER.base_url`，通过统一 `SemanticServiceClient` 从 Semantic Service 拉取 schema、字段语义、表描述和 join 信息；Validator 启用 `metadata_match` 时仅做字段合法性等元数据校验，不再调用额外字面值校验服务。

专用 NL2SQL Agent 的关键配置是：

```yaml
AGENT_CONFIG:
  type: "nl2sql"

DATABASE:
  db_id: "<your_db_id>"
  engine: "sqlite"
  config:
    path: "/path/to/your.sqlite"

SEMANTIC_LAYER:
  base_url: "http://localhost:32000"
  username: "example"
  password: "123456"
  timeout: 30
  verify_ssl: false
```

完整配置、运行命令和排查方式请参考：[构建 NL2SQL 专用 Agent](../case/build-an-nl2sql-application.md)。

### 3.3 在 NL2SQL 子 Agent 中使用

通用 ReAct 主 Agent 如果只在需要 SQL 查询时调用 NL2SQL，推荐注册 `nl2sql_sub_agent_tool`。这个工具会读取内置源配置：

```text
dataagent/agents/nl2sql/nl2sql_agent.yaml
```

运行时再把**主 Agent 当前配置中的 `DATABASE` 和 `SEMANTIC_LAYER` 覆盖写入临时 NL2SQL 子 Agent YAML**，然后通过 `sub_agent_tool` 启动 NL2SQL 子 Agent。因此，主 Agent 需要配置自己的 `DATABASE` 和 `SEMANTIC_LAYER`，不需要直接修改源 NL2SQL YAML。

主 Agent 侧只需要关注三类配置：

| 配置 | 作用 |
| --- | --- |
| `TOOLS.local_functions[].function: nl2sql_sub_agent_tool` | 注册 NL2SQL 子 Agent 工具。 |
| `DATABASE` | 指定主 Agent 当前业务数据库，运行时覆盖给 NL2SQL 子 Agent。 |
| `SEMANTIC_LAYER` | 指定 Semantic Service REST 服务，运行时覆盖给 NL2SQL 子 Agent。 |

`nl2sql_sub_agent_tool` 会做三件关键事情：

1. 读取 `dataagent/agents/nl2sql/nl2sql_agent.yaml` 作为 NL2SQL 子 Agent 基础配置。
2. 从主 Agent 的 `config_manager` 读取 `DATABASE` 和 `SEMANTIC_LAYER`，覆盖到临时子 Agent YAML。
3. 如果工具配置中设置了 `config.llm_model`，从主 Agent 的 `MODEL.<llm_model>` 读取模型配置，并将该模型作为子 Agent 的模型配置。

因此，主 Agent 中的配置是 NL2SQL 子 Agent 实际运行时的最终来源：

| 主 Agent 配置 | 子 Agent 中的效果 |
| --- | --- |
| `DATABASE` | 覆盖 NL2SQL 子 Agent 的数据库配置 |
| `SEMANTIC_LAYER` | 覆盖 NL2SQL 子 Agent 的 Semantic Service 配置 |
| `TOOLS.local_functions[].config.llm_model` | 绑定子 Agent 使用的模型槽位 |
| `MODEL.<llm_model>` | 写入临时子 Agent YAML 的 `MODEL` |

完整主 Agent YAML、工具参数、运行方式和排查方式请参考：[构建数据分析 Agent](../case/build-a-dataagent-from-scratch.md)。

### 3.4 Semantic Service 提供给 NL2SQL 的能力

NL2SQL 通过 `dataagent/actions/tools/semantic_tool/semantic_client.py` 调用 Semantic Service：

| 能力 | 作用 |
| --- | --- |
| `get_table_list(db)` | 获取数据库中的表和表描述。 |
| `get_table_columns_info(table_name)` | 获取表字段、字段描述、字段类型和示例值。 |
| `get_joinable_tables(table_names)` | 获取表之间可 join 的字段关系。 |
| `semantic_search_columns(db, keywords, top_k)` | 根据关键词语义召回相关字段。 |
| `vector_search_table_desc(db, keywords, top_k)` | 根据表描述向量召回相关表。 |

默认流程中，如果没有提供固定 `user_schema`，NL2SQL Perceptor 会从 Semantic Service 获取 schema，并把表、字段和 join 信息转换成模型可读的 SQL 上下文。

## 4. Ontology 本体能力

Ontology 本体服务面向业务知识图谱。和增强元数据 REST 能力不同，本体不是为 SQL 生成提供表字段上下文，而是用于表达业务对象、对象关系、属性约束、路径规则、统计口径和服务端动作等业务语义。

本体服务属于开发中的 Semantic Service 能力。相关服务实现、`ontology_service` Skill、CLI 脚本和本体查询示例会在能力稳定后补充；当前文档先说明能力目标和接入边界。

开发中的 Ontology 能力包括：

| 能力 | 说明 |
| --- | --- |
| Schema 发现 | 查询当前场景中的实体类型、关系类型、节点属性和边属性。 |
| 实体查询 | 按对象类型列出节点实例，或根据 UUID 查询节点详情。 |
| 关系查询 | 查询关系类型、边实例，以及起点/终点相关的一跳关系。 |
| 属性过滤 | 按属性条件过滤节点或边，例如名称包含、数值范围、枚举匹配等。 |
| 属性解释 | 查询节点或边的属性名、属性含义和属性值，帮助 Agent 理解字段语义。 |
| 路径查询 | 做多跳查询、子图查询或起点-关系-终点模式查询。 |
| 统计聚合 | 对满足条件的节点或边做数量统计、排序和数值聚合。 |
| 服务端 action | 查询服务端声明的 action，并在参数明确后执行 action。 |

### 4.1 规划中的接入方式

未来本体服务开源或对接后，推荐仍以确定性工具或 Skill 的方式暴露给主 Agent，而不是让模型直接猜测本体标签、属性名、UUID 或 action 参数。推荐流程是：

1. 先发现当前业务场景中的实体类型、关系类型和可查询属性。
2. 根据用户问题解析候选业务对象、关系和过滤条件。
3. 在本体服务中确认对象标识、属性含义和关系边界。
4. 对明确后的对象执行关系查询、路径查询、统计聚合或服务端 action。
5. 将查询依据和结果返回给主 Agent，用于回答或作为后续 NL2SQL 查询的业务约束。

以上流程是开发中的设计方向。对应命令、环境变量和服务地址配置会随本体服务能力稳定后补充到文档中。

## 5. 能力选择建议

在实际业务 Agent 中，可以按任务类型选择 Semantic Service 能力：

| 用户问题类型 | 推荐处理方式 |
| --- | --- |
| “查某张业务表并统计指标” | 主 Agent 调 `nl2sql_sub_agent_tool`，Semantic Service 为 NL2SQL 提供 schema 和 join 信息。 |
| “这个业务对象有哪些关联对象” | 属于开发中的 Ontology 本体服务场景；现阶段建议先由用户提供明确对象和关系约束。 |
| “先确认业务对象，再查对应数据表统计结果” | 当前可先通过业务侧规则或人工约束明确对象，再把明确后的查询目标交给 NL2SQL 子 Agent；本体自动确认能力属于后续规划。 |
| “只做自然语言转 SQL” | 直接使用 `type: "nl2sql"` 的 NL2SQL Agent。 |

完整落地教程请参考：

- [构建 NL2SQL 专用 Agent](../case/build-an-nl2sql-application.md)
- [构建数据分析 Agent](../case/build-a-dataagent-from-scratch.md)

## 6. 配置检查清单

- NL2SQL 单独运行时，确认 `AGENT_CONFIG.type: "nl2sql"`。
- 主 Agent 调 NL2SQL 子 Agent 时，确认注册的是 `nl2sql_sub_agent_tool`，而不是通用 `sub_agent_tool`。
- Semantic Service 配置写在运行时 Agent 的 `SEMANTIC_LAYER` 下；子 Agent 场景中写在主 Agent YAML 中即可。
- `DATABASE.db_id` 必须与 Semantic Service 中导入的数据库名一致。
- `SEMANTIC_LAYER.base_url` 建议写 `http://host:port`，客户端会统一补齐到 `/api/semantic/v1`。
- 本体/知识图谱查询能力正在开发中；相关接入能力稳定后，再按文档配置 `ontology_service`、`ONTOLOGY_URL` 或 `SCENE`。

## 7. 相关代码和示例

- NL2SQL Agent 配置：`dataagent/agents/nl2sql/nl2sql_agent.yaml`
- NL2SQL Perceptor：`dataagent/agents/nl2sql/nodes/perceptor.py`
- Semantic Service 统一客户端：`dataagent/actions/tools/semantic_tool/semantic_client.py`
- NL2SQL Validator：`dataagent/agents/nl2sql/nodes/validator.py`
- NL2SQL 子 Agent 工具：`dataagent/actions/tools/local_tool/tools.py`
- NL2SQL 子 Agent 主 Agent 示例：`dataagent/core/flex/examples/nl2sql_flex_e2e_subagent.yaml`
