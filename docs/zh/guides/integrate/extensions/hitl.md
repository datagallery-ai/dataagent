# HITLHandler 与 HITLMiddleware

模型需要澄清需求，或策略需要在指定阶段暂停运行时，可以请求人工反馈。`HITLHandler` 决定何时提问、问什么以及如何使用回答。`HITLMiddleware` 保存待处理的交互、暂停运行图，并在收到回答后恢复。使用异步 `ainvoke` 或 `astream` 运行。

本页介绍通用机制。内置 SQL 与 IR 的交互策略见 [de-agent 人工反馈](de-agent-hitl.md)。

<a id="_1" name="_1"></a>
<a id="启用和交互提示词" name="启用和交互提示词"></a>

<a name="enable-and-supply-instructions"></a>

## 启用和交互提示词

插件 `.plugin.yaml` 中的 `hitl` 声明为默认根 Agent 启用人工反馈。

```yaml
hitl:
  enabled: true
  instructions: prompts/hitl.md
  policies: []
```

插件编译器读取相对于插件根目录的 Markdown 文件，将内容加到根 Agent 的系统提示词中。文件为空、缺失或超出插件根目录时装配失败。省略 `instructions` 也可以运行策略。关闭该插件的 HITL 会跳过它的提示词和 handler，其他已启用来源仍可使用。字段和默认值见[人工交互配置参考](../../../reference/extension-schema.md#hitl-configuration)。

已启用插件共用 `request_human_feedback` 工具。提示词可以引导模型自主调用。下面的参数用于请用户选择去重主键。

```json
{
  "question": "活跃用户按哪个标识去重？",
  "reason": "需求未指定标识字段。",
  "options": ["user_id", "device_id"]
}
```

工具等待非空文本，并以成功的 `ToolMessage` 返回原始回答。如果模型同一次响应还提出其他工具调用，只执行第一条反馈调用。收到回答后，模型重新规划。该次模型响应跳过确定性的 `after_model` 策略，反馈工具自身的调用跳过 `before_tool` 和 `after_tool` 策略。

<a id="_2" name="_2"></a>
<a id="三个方法及默认实现" name="三个方法及默认实现"></a>

<a name="handler-methods"></a>

## 三个方法及默认实现

| 方法 | 返回类型 | 默认行为 |
| --- | --- | --- |
| `trigger_policy(context, *, params)` | `TriggerDecision` | 每次匹配的阶段或工具调用都会触发提问 |
| `interaction_builder(context, *, params)` | `InteractionRequest` | 使用 `params.question` 和 `params.options`，缺少问题时使用通用问题 |
| `resume_handler(context, *, params)` | `ResumeDecision` | 将回答作为反馈交给模型重新规划，业务状态保持原值 |

按需用 `def` 或 `async def` 重写方法。将以下类保存到插件的 `hitl/clarification.py`，用于自定义一个固定的澄清问题。

```python
from dataagent import HITLHandler, InteractionRequest


class ClarificationHandler(HITLHandler):
    def interaction_builder(self, context, *, params):
        return InteractionRequest(
            question="活跃用户应该按哪个标识去重？",
            options=["user_id", "device_id"],
        )
```

| 阶段 | 时机 |
| --- | --- |
| `before_agent` | 一次执行开始时，在首次模型调用前运行一次 |
| `before_model` | 每次模型调用前 |
| `after_model` | 模型响应后、执行计划中的工具前 |
| `before_tool` | 单个工具执行前，包括 `task` |
| `after_tool` | 单个工具返回后，父图提交结果前 |
| `after_agent` | 沿结束路径返回前。重新规划可以进入模型循环，再次接受复审 |

`HITLContext` 提供阶段 `event`、完整的 `policy_id`、分离的 `state` 和原生 `runtime`。从 `runtime.context` 读取宿主服务。工具阶段还提供 `tool_call`，`after_tool` 的 `tool_result` 保存原生结果，状态预览含工具的业务更新。问题构建方法收到触发判定 `decision`，恢复方法还能读取交互 `interaction` 与回答 `answer`。handler 实例会被共享，应显式返回状态更新。

<a id="yaml" name="yaml"></a>
<a id="yaml-及显式装配" name="yaml-及显式装配"></a>

<a name="manifest-and-explicit-assembly"></a>

## YAML 及显式装配

插件清单注册前面定义的类，并指定执行阶段。

```yaml
hitl:
  policies:
  - id: clarification
    version: '1'
    at: before_tool
    matcher: task
    handler: hitl/clarification.py:ClarificationHandler
    params: {}
```

`handler` 相对于插件根目录，指向可无参构造的 `HITLHandler` 子类。省略时使用基类。若要匹配特定子代理，声明 `matcher: task`，再在 `trigger_policy` 中检查 `subagent_type`。编译器用插件 ID 作为命名空间。`my-plugin/clarification` 这样的完整策略 ID 必须唯一。

DataAgent 将已启用插件的策略汇集到默认根图上的一个中间件。自行装配 Deep Agents 图时，由调用方提供模型、工具、子代理和 checkpointer。示例使用内存 checkpointer。

```python
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from dataagent import read_yaml
from dataagent.core.graph import create_deep_agent
from dataagent.extensions.hitl import HITLMiddleware

path = Path("/path/to/plugin/.plugin.yaml")
section = read_yaml(path)["hitl"]
middleware = HITLMiddleware.from_config(section, root=path.parent, namespace="main")
graph = create_deep_agent(
    model=model,
    tools=tools,
    subagents=subagents,
    middleware=[middleware],
    checkpointer=InMemorySaver(),
)
```

显式装配时，需要由调用方把提示词传给图。标准装配时插件编译器会读取清单中的 `instructions`。如果要跨进程重启恢复，请使用持久化 checkpointer。

<a id="_3" name="_3"></a>
<a id="回答与恢复" name="回答与恢复"></a>

<a name="answer-and-resume"></a>

## 回答与恢复

- **Web。** 输入文字或选择显示的选项。重新打开会话可以取回待回答的问题。
- **TUI。** 下一次手动输入用于回答当前问题，排队的输入继续等待。通过 `/resume <threadId>` 重新打开会话。
- **HTTP 和 A2A。** 将答案和待处理的 interrupt ID 交给原请求入口。请求格式和相应宿主的重启行为见 [AG-UI 人工交互契约](../../../reference/events.md#hitl)或 [A2A 人工交互契约](../../../reference/a2a.md#a2a-hitl)。
- **嵌入式运行图。** 读取 interrupt，在同一线程上提交 `Command(resume=...)`。

```python
from langgraph.types import Command

config = {"configurable": {"thread_id": "clarification-example"}}
result = await graph.ainvoke(
    {"messages": [("user", "请将活跃用户统计交给子代理处理")]},
    config,
)
for pending in result.get("__interrupt__", ()):
    print(pending.value["message"])
# 收到回答后提交。如果有多条待回答项，使用与回答对应的 ID。
if result.get("__interrupt__"):
    pending = result["__interrupt__"][0]
    result = await graph.ainvoke(Command(resume={pending.id: "user_id"}), config)
```

回答使用非空文本。空白或结构化回答会再次暂停并返回 `validation_error`。选项是提示，业务校验在 `resume_handler` 中完成。多个并行工具可以分别暂停，提交时按 interrupt ID 匹配回答。中间件会等待调用方输入，当前没有自动回答和交互超时。

<a name="resume-actions"></a>

### 恢复动作与状态更新

| 阶段 | `continue` | `replan` | `reject` |
| --- | --- | --- | --- |
| before_agent / before_model | 携带反馈进入后续模型流程 | 携带反馈继续且不自循环 | 转入结束路径并给出原因 |
| after_model | 保留当前计划 | 跳过计划中的工具并返回模型 | 跳过计划中的工具并转入结束路径 |
| before_tool | 所有匹配策略通过后执行 | 跳过本次调用并将反馈交给模型 | 拒绝本次调用并将原因交给模型 |
| after_tool | 保留实际结果 | 保留实际结果并将反馈交给模型 | 调用已执行，将拒绝原因交给模型 |
| after_agent | 正常结束 | 携带反馈重新进入模型循环 | 带着拒绝原因结束 |

拒绝单次工具调用后，会话仍可继续。需要持续执行的业务限制应放进触发器或执行检查。状态阶段进入结束路径后仍会经过原生 `after_agent`。跳过计划时为相应工具调用 ID 生成错误 `ToolMessage`。before-tool 阶段保留原调用参数。`return_direct` 工具在 `continue` 时直接返回，在 `replan` 或 `reject` 时转回模型。默认触发器可能在重新规划后再次提问。只问一次需要重写触发器。

要写入业务状态，可在 `ResumeDecision` 中返回更新。示例将回答保存为 `selected_key`。

```python
return ResumeDecision(
    action="continue",
    state_update={"selected_key": context.answer},
)
```

在状态 schema 中声明该字段。中间件会合并各 handler 兼容的 `state_schema` 字段。before-tool 更新在工具的 `ToolRuntime.state` 中可见。若工具实际执行，更新按 before-tool、工具、after-tool 的顺序提交，字段定义了 reducer 时按该规则合并。返回 `replan` 或 `reject` 时也会提交显式更新。直接修改分离的 state 不会提交。

<a name="recovery"></a>

### Checkpoint 与恢复

待回答交互会保存策略 ID、版本、阶段、问题和参数。存在待回答项时，保留原策略 ID、阶段和版本。工具执行完毕后从 after-tool 阶段恢复会复用已完成的结果。handler 可能重放，执行中断的工具可能重试，所以有外部副作用时要做到幂等。持久化 prepare/execute 任务、reducer 和重放测试见[人工反馈的执行与恢复](../../../../development/architecture/execution.md#hitl-recovery)。

<a id="ir" name="ir"></a>
<a id="guard" name="guard"></a>
<a id="ir-子代理示例与恢复范围" name="ir-子代理示例与恢复范围"></a>
<a id="结束前的两个-guard" name="结束前的两个-guard"></a>

原 IR 与恢复章节的内容见 [de-agent 策略](de-agent-hitl.md#completion-guards)与[通用恢复契约](#recovery)。
