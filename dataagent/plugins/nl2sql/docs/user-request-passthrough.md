# 设计：把用户原话确定地传给 nl2sql

状态：提案，未实施
范围：单轮。多轮见第 9 节

## 1. 背景

在 BIRD MiniDev 的 1473 题上，端到端回答错误。

```text
用户：What was the average monthly consumption of customers in SME for the year 2013?
      hint: Average Monthly consumption = AVG(Consumption) / 12; Year 2013 can be presented as Between 201301 And 201312; ...
正确答案：459.956     产品回答：5519.48
```

从 Trace 逐个节点检查后，错误不在 nl2sql：

| 步骤 | 是否包含公式 `/ 12` |
| --- | --- |
| 用户消息 | 包含 |
| 主 Agent 调用 `task` 时写的 `description` | **丢失**。改写成“求 SUM、AVG、COUNT 三个值” |
| nl2sql 的 generator、validator、selector | 不包含 |
| nl2sql 生成的 SQL | 正确回答了改写后的问题 |
| 主 Agent 的最终回答 | 直接用 AVG，没有再除以 12 |

主 Agent 违反了 `nl2sql.description.md` 已有的两条要求：带上指标定义，保留用户原话。

## 2. 根因

主 Agent 和 nl2sql 之间只有一段自由文本。

1. `task` 工具的参数只有 `description` 和 `subagent_type`，所有子代理共用（`dataagent/core/middleware/subagents/subagents.py` 中的 `TaskToolSchema`）。nl2sql 不能声明自己的结构化参数。
2. nl2sql 以 isolated 模式运行。`_child_state` 只给它 `messages=[HumanMessage(description)]`，看不到用户原话。
3. nl2sql 的 `_question_from_task` 把最后一条 HumanMessage 当作问题。

所以信息是否完整，取决于主 Agent 一次转述的质量。提示词约束只能提高概率，不能保证。

## 3. 参考做法

| 做法 | 来源 |
| --- | --- |
| 共享完整上下文，不只传摘要 | Cognition，“Don't Build Multi-Agents” |
| 交接时默认传完整对话历史，用 `input_filter` 裁剪 | OpenAI Agents SDK，Handoffs |
| 委派时写清目标、格式、边界；子 Agent 产出写到文件，只传引用，减少“传话游戏” | Anthropic，“How we built our multi-agent research system” |
| SQL 服务接收完整对话历史，自己改写追问，并展示“我们这样理解你的问题”；指标公式写在语义模型里 | Snowflake Cortex Analyst 文档 |

共同点：执行者直接看到原话，转述只作补充。

nl2sql 的返回方向已经这样做：完整结果写入 `result_file`，只回传引用。本方案处理输入方向。

## 4. 目标

| 目标 | 验证方式 |
| --- | --- |
| 用户本轮原话原样进入 nl2sql 的 generator 和 validator 提示词，不经过模型转述 | 单元测试；1473 的 Trace |
| 原话和任务描述冲突时，定义、公式、过滤值以原话为准 | 1473 端到端结果为 459.956 |
| 直接调用 nl2sql（benchmark runner、单元测试）时行为不变 | 现有测试全部通过 |
| 其他编译型插件不受影响 | `mode` 可省略，省略时行为不变 |

不在本次范围：多轮指代与定义继承、问题整理节点、语义层指标（见第 9 节）。

## 5. 方案

宿主已经支持编译型子代理的 fork 模式：`_child_state` 会传入父对话历史。缺的是插件声明不能写 `mode`。

```text
父对话 messages:
  [Human] What was the average monthly ... AVG(Consumption) / 12 ...
  [AI]    tool_call task(description="...SUM, AVG, COUNT...")      ← 转述可能丢信息
                  │
                  ▼  _child_state（fork）：复制历史，去掉未完成的 tool_call，追加任务描述
子代理 state["messages"]:
  [Human] What was the average monthly ... AVG(Consumption) / 12 ...   ← 用户原话
  [Human] ...SUM, AVG, COUNT...                                         ← 任务描述（编译型不加前导语）
                  │
                  ▼  nl2sql._task_inputs(state)
  question     = 最后一条 HumanMessage
  user_request = 它之前最近的一条 HumanMessage
                  │
                  ▼  generator / validator 提示词
  ## User's original request:    ← 定义、公式以此为准
  ## Question:                   ← 回答范围以此为准
```

### 5.1 宿主改动

**H1. 插件声明支持 `mode`。** 文件 `dataagent/config/declarations.py`，`SubAgentSpec`：

```python
mode: Literal["isolated", "fork"] | None = None
```

`StrictModel` 拒绝未知字段，不加这个字段，`nl2sql.yaml` 写 `mode` 会启动失败。

**H2. 编译时传递 `mode`。** 文件 `dataagent/extensions/subagents.py`，`_compiled_child` 返回值：

```python
child = {"name": spec.name, "description": description, "runnable": result}
if spec.mode is not None:
    child["mode"] = spec.mode
return child
```

**H3. 编译型 fork 不加给模型看的提示。** 文件 `dataagent/core/middleware/subagents/subagents.py`。

fork 模式现在有两处附加文字，都是写给“接着父对话继续工作的 LLM Agent”的，对编译型图不合适：

| 附加文字 | 位置 | 对 nl2sql 的问题 | 改法 |
| --- | --- | --- | --- |
| 工具列表注释 `(inherits your full conversation ... no need to restate context here)` | `_join_listings` 调 `_listing_line` | 主 Agent 会把任务描述写短；而 nl2sql 靠任务描述确定回答范围 | `continues=_continues_parent(spec)` 改为 `continues=_is_forked_subagent(spec)`，只对标准 fork 加注释 |
| 任务前导语 `_FORK_TASK_PREAMBLE` | `_history_for_fork` | 前导语进入最后一条消息，会被当成问题的一部分写进 SQL 提示词 | 编译型 fork 不加前导语，只追加 `HumanMessage(description)` |

第二项不放在插件里处理。插件按设计不依赖宿主模块，拿不到前导语常量；按格式截取字符串容易出错。

`_history_for_fork` 改为由调用方决定是否加前导语：

```python
def _history_for_fork(messages, event, description, *, preamble: bool = True):
    ...
    task = _FORK_TASK_PREAMBLE + description if preamble else description
    return [*folded, HumanMessage(content=task)]
```

`_child_state` 中，编译型 fork 传 `preamble=False`。

返回路径不变：`_command_from_result` 仍然只回传一条 `ToolMessage`。

### 5.2 插件改动

**P1. 声明 fork。** 文件 `subagents/nl2sql.yaml`：

```json
{
  "kind": "compiled",
  "name": "nl2sql",
  "mode": "fork",
  "description_file": "subagents/nl2sql.description.md",
  "entrypoint": "subagents/nl2sql.py:build_subagent"
}
```

**P2. 取出两路输入。** 文件 `subagents/nl2sql.py`，用 `_task_inputs` 替换 `_question_from_task`：

```python
def _task_inputs(state: Mapping[str, Any]) -> tuple[str, str]:
    """(question, user_request)。

    question 是最后一条 HumanMessage，即主 Agent 写的任务描述。
    user_request 是它之前最近的一条 HumanMessage，即用户本轮原话；
    直接调用时只有一条消息，user_request 为空，行为与 isolated 相同。
    """
    texts = [
        text
        for message in state.get("messages") or []
        if isinstance(message, HumanMessage) and (text := _message_text(message))
    ]
    if not texts:
        raise DataAgentError(...)  # 与现有报错相同
    return texts[-1], texts[-2] if len(texts) > 1 else ""
```

`run_task` 中：

```python
question, user_request = _task_inputs(state)
final = await workflow.ainvoke(
    get_default_state(question=question, user_request=user_request, **defaults),
    config=_run_config(config),
)
```

结果文件的 `meta.json` 同时记录 `user_request`，便于排查。

**P3. state 与节点上下文。**

| 文件 | 改动 |
| --- | --- |
| `nl2sql/state.py` | `NL2SQLState` 加 `user_request: str`；`get_default_state` 默认 `""` |
| `nl2sql/nodes/generator.py` | `context` 加 `"user_request": state["user_request"]` |
| `nl2sql/nodes/validator.py` | `_validate_semantic` 的 `context` 加同一项 |

**P4. 提示词与描述。**

`prompts/generator/*_user.md` 和 `prompts/validator/validate_semantic_user.md`，在 `## Question` 前加：

```jinja
{% if user_request %}
## User's original request:
{{ user_request }}
{% endif %}
```

对应的 system 提示词加一条规则：

> **Question** defines what to answer. **User's original request** is authoritative for definitions, formulas, filter values and wording. If they conflict, follow the user's original request.

`subagents/nl2sql.description.md`：

- 删去 “The subagent cannot see the conversation.”
- 改为：“It also receives the user's latest message verbatim. Still write the task as a complete, standalone question.”
- 补充计算归属：“When one SQL query can compute the requested metric, pass the full formula; the SQL returns the final value.”（对应 1473 中主 Agent 拆走公式又忘记计算的问题）

## 6. 兼容性

| 场景 | 行为 |
| --- | --- |
| 其他编译型插件，不写 `mode` | 不变 |
| 标准子代理（fork 或 isolated） | 不变；工具注释与前导语照旧 |
| nl2sql 被直接调用（runner、单元测试） | 只有一条消息，`user_request` 为空，行为与现在相同 |
| nl2sql 经主 Agent 调用 | 新增原话输入 |

## 7. 风险

| 风险 | 说明 | 处理 |
| --- | --- | --- |
| 成本 | fork 把父历史交给子图 | nl2sql 只取最近一条用户消息送进提示词，不送历史；新增 token 约等于一条用户消息的长度 |
| 一次提问拆成多个任务 | 原话覆盖整个问题，每个任务只回答其中一部分 | 规则写明：回答范围以 Question 为准，原话只决定定义和措辞 |
| 中间件注入的 HumanMessage | 如果其他中间件在父历史中插入 HumanMessage，`texts[-2]` 可能不是用户原话 | 单元测试覆盖；必要时改为按消息来源识别 |
| 文档 | `docs/extensions/subagents.md` 与 `docs/reference/extension-schema.md` 需补 `mode` 字段 | 与代码同一次提交 |

## 8. 验证

| 步骤 | 通过标准 |
| --- | --- |
| 单元测试：fork 状态 | `question` 等于任务描述且不含前导语；`user_request` 等于用户原话 |
| 单元测试：isolated 状态 | `user_request` 为空 |
| 单元测试：工具说明 | nl2sql 的说明中没有 “no need to restate” |
| 回归 | `tests/test_nl2sql_plugin.py`、`tests/test_compiler*.py`、`tests/test_declarations.py` 全部通过 |
| 端到端：1473 | Trace 中 generator 提示词含 `/ 12`；回答为 459.956 |
| 端到端：其余 4 题（1483、1484、1472、1479） | 结果与 gold 一致，且没有比改动前变差 |

## 9. 后续：多轮

只看本轮原话在多轮中不够：

```text
第 1 轮：2013 年 SME 客户的月均消费？月均消费 = AVG(Consumption) / 12
第 2 轮：那 LAM 呢？
第 3 轮：换成 2012 年
```

第 3 轮的原话不含公式和客户分组。计划在本方案基础上增加：

1. **三路输入**：任务描述（回答范围）、本会话所有用户原话（定义，数量有上限）、之前的查询记录（问题 + SQL）。
2. **问题整理节点**：在 perceptor 之前，输出完整问题、带出处的约束列表和基础 SQL。规则：原话优先；同一维度新值覆盖旧值；未被覆盖的定义继续生效；无法确定时写进回复，不猜测。
3. **查询记录持久化**：fork 历史会先应用摘要事件，长会话的早期原话可能被压缩。查询记录优先从 `<query_id>.meta.json` 读取，不依赖对话历史。
4. **评测**：BIRD 只有单轮题。从单轮题构造追问，例如 1473 之后接“那 LAM 呢？”，gold 为把 `'SME'` 改为 `'LAM'`，检查定义继承和覆盖规则。

更长期：回复中加入 `interpretation`，由主 Agent 展示给用户；业务指标定义写入语义层，不依赖对话传递。

## 附录：改动清单

| 侧 | 文件 | 改动 |
| --- | --- | --- |
| 宿主 | `dataagent/config/declarations.py` | `SubAgentSpec.mode` |
| 宿主 | `dataagent/extensions/subagents.py` | `_compiled_child` 传递 `mode` |
| 宿主 | `dataagent/core/middleware/subagents/subagents.py` | 编译型 fork 不加工具注释和前导语 |
| 宿主 | `docs/extensions/subagents.md`、`docs/reference/extension-schema.md` | 补 `mode` |
| 插件 | `subagents/nl2sql.yaml` | `"mode": "fork"` |
| 插件 | `subagents/nl2sql.py` | `_task_inputs`；`run_task` 传 `user_request`；meta 记录原话 |
| 插件 | `nl2sql/state.py` | `user_request` 字段 |
| 插件 | `nl2sql/nodes/generator.py`、`nl2sql/nodes/validator.py` | 上下文加 `user_request` |
| 插件 | `prompts/generator/*`、`prompts/validator/validate_semantic_*` | 原话段落与优先级规则 |
| 插件 | `subagents/nl2sql.description.md` | 删去“看不到对话”；补计算归属 |
| 测试 | `tests/test_nl2sql_plugin.py` 等 | 第 8 节的新增用例 |
