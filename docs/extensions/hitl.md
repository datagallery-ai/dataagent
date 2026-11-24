# HITLHandler 与 HITLMiddleware

两者与交互数据类型集中在 `dataagent/extensions/hitl.py`：

- `HITLHandler` 描述业务行为，可以按需重写三个方法。
- `HITLMiddleware` 负责准备交互、保存 checkpoint、interrupt、收集回答和恢复执行。

当前支持异步 `ainvoke/astream`，以及以下六个 `at` 阶段：

| 阶段 | 时机 |
| --- | --- |
| `before_agent` | 本次 agent 执行开始、首次模型调用前 |
| `before_model` | 每次模型调用前 |
| `after_model` | 每次模型返回后、执行计划中的工具前 |
| `before_tool` | 单个工具执行前，包括 `task` 子代理入口 |
| `after_tool` | 单个工具已返回，结果提交到父图之前 |
| `after_agent` | agent 沿结束路径返回前 |

模型循环不重复执行 before_agent；after_agent 返回 replan 时可以重新进入模型循环，
并在再次结束时接受复审。当前仍提供显式 Python 装配接口；
插件 `.plugin.yaml` 中的 hitl 会自动安装到父 agent，支持 instructions 自主提问。
HITL 配置统一声明在插件 `.plugin.yaml` 的 hitl 字段，guard 不从主配置的
plugins.config 读取交互参数。自动应答和交互超时尚未接入。

Web 与 TUI 已支持通过 HTTP 暂停和恢复：Web 展示问题、候选项和文本输入；TUI
显示问题后，将下一次手动输入作为回答，排队中的任务会继续等待。刷新 Web 或通过
TUI `/resume <threadId>` 可恢复待回答项。公开协议只返回 `type`、`interruptId`、
`message` 以及可选 `options`、`validationError`；策略内部信息仍保留在 checkpoint。
详见 [HTTP 接口](../reference/rest-api.md) 与 [事件](../reference/events.md)。

## 启用和交互提示词

```yaml
hitl:
  enabled: true
  instructions: prompts/hitl.md
  policies: []
```

`enabled` 默认 true，保留已有策略配置的行为。未声明 hitl 的插件不会启用此能力。
设为 false 时，该来源不加载 handler、不读取 instructions、不注册反馈工具；其他
启用插件的 HITL 不受影响。配置结构本身仍会校验。

`instructions` 为相对插件目录的 Markdown 文件路径，不是内联提示词。启用后读取
并注入父 agent 的系统提示词，空文件、缺失文件或越界路径均报错。省略时仍可使用
policies，也可以由其他提示词指导模型自主调用工具。多个启用来源共用一把工具：

```python
request_human_feedback(
    question="活跃用户按哪个标识去重？",
    reason="需求中未指定标识字段",
    options=["user_id", "device_id"],
)
```

工具通过原生 interrupt 等待非空文本，恢复后以原始回答作为成功 ToolMessage 返回
模型，不自动确认或修改 IR。同一轮若还提出其他工具调用，只保留第一条反馈调用，
其余调用丢弃，待用户回答后由模型重新规划。自主提问不再额外触发同轮的确定性
after_model guard，也不会递归进入 before_tool/after_tool HITL。

旧版 human_feedback_conditions、Initial/Mid/Final 阶段与相关交互纪律已整理到
[de-agent HITL instructions](../../dataagent/plugins/de-agent/prompts/hitl.md)，旧工具名称
已保留，参数说明统一为当前的 question/reason/options。

## 三个方法及默认实现

| 方法 | 返回类型 | 默认行为 |
| --- | --- | --- |
| `trigger_policy(context, *, params)` | `TriggerDecision` | 每次匹配的工具调用都触发 |
| `interaction_builder(context, *, params)` | `InteractionRequest` | 使用 params.question/options，否则生成通用问题 |
| `resume_handler(context, *, params)` | `ResumeDecision` | 将用户回答返回模型重新推理，不修改业务状态 |

方法可使用 `def` 或 `async def`。例如只定制问题：

```python
from dataagent import HITLHandler, InteractionRequest

class ClarificationHandler(HITLHandler):
    def interaction_builder(self, context, *, params):
        return InteractionRequest(
            question="活跃用户应该按哪个标识去重？",
            options=["user_id", "device_id"],
        )
```

`context.event` 表示阶段。`context` 还提供当前工具调用 `tool_call`、策略完整 ID `policy_id`、业务 `state`、
`runtime`、trigger 返回的 `decision`、交互 `interaction` 和用户 `answer`。
后两个字段仅在相应阶段可用；`state` 是深拷贝，直接修改不会提交到图。
agent/model 阶段的 `tool_call` 为 None；after_tool 额外提供原生 `tool_result`
（ToolMessage 或 Command），其 `state` 预览包含工具返回的业务更新和结果消息。
业务服务可从原生 `runtime.context` 获取，业务状态应使用 detached `context.state`。
handler 实例可被多个工具并发使用，不要在 `self` 上保存某次交互的可变状态。

## YAML 及显式装配

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

通常由插件编译器自动加载；需要在 SDK 中显式装配时，读取同一个 `.plugin.yaml`
的 `hitl` 部分：

```python
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from dataagent.config.strict_yaml import read_yaml
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

`handler` 是相对 `root` 的 `file.py:ClassName`，引用 `HITLHandler` 的无参构造子类。
省略 `handler` 使用基类；固定问题也可直接写在 `params.question/options` 中。
`at` 默认 before_tool。`matcher` 仅用于 before_tool/after_tool，精确匹配工具名，
省略时检查全部工具。在 agent/model 阶段配置 matcher 会报错。它不能匹配子代理名称；
要检查 nl2sql，匹配 `task`，再在 `trigger_policy` 检查 `subagent_type` 参数。

`namespace` 与策略 `id` 组成完整 ID，例如 `main/clarification`、`de-agent/ir_gate`。
同一 middleware 中完整 ID 必须唯一；策略来源的自动合并与 override 尚未实现。

## 回答与恢复

```python
from langgraph.types import Command

config = {"configurable": {"thread_id": "ir-example"}}
result = await graph.ainvoke({"messages": [("user", "生成活跃用户统计 SQL")]}, config)
pending = result["__interrupt__"][0]
print(pending.value["message"])
result = await graph.ainvoke(Command(resume={pending.id: "user_id"}), config)
```

middleware 目前接受非空文本回答；空白或结构化对象会再次等待并提供
`validation_error`。`options` 仅供展示，业务候选值校验由子类在 `resume_handler`
中决定，例如无法确认时返回 `replan` 请求继续澄清。本阶段没有 `response_handler`。

| 阶段 | `continue` | `replan` | `reject` |
| --- | --- | --- | --- |
| before_agent / before_model | 携带反馈进入后续模型流程 | 携带反馈进入后续模型流程，不自循环 | 转入结束路径，返回拒绝说明 |
| after_model | 保留本轮计划，继续执行或结束 | 跳过本轮全部工具，回到模型 | 跳过本轮全部工具，转入结束路径 |
| before_tool | 全部命中策略通过后执行原工具 | 跳过原工具，将反馈返回模型 | 拒绝原调用，将原因返回模型 |
| after_tool | 保留真实工具结果，沿原流程继续 | 保留真实结果，将反馈返回模型 | 拒绝结果，将原因返回模型；不会撤销执行 |
| after_agent | 沿结束路径返回结果 | 携带反馈重新进入模型循环 | 返回拒绝说明，沿结束路径结束 |

工具阶段的 reject 不结束整个会话；需要持久禁止再次执行的业务规则，应在 trigger
或执行检查中另行体现。状态阶段转入结束路径后仍会经过原生 after_agent 链。
after_model 跳过计划时会生成对应的 error ToolMessage，确保模型消息中的调用都有结果，
不会留下未完成的 tool_call。before_tool 的调用参数始终保留，不自动改写。
return_direct 工具在 continue 时保留原生直接返回行为，在 replan/reject 时转回模型。

默认 trigger 每次都会触发；默认 replan 后如果再次到达同一阶段，会再次提问。
只问一次或 IR 确认后放行，应重写 `trigger_policy`。

本阶段需要提交业务状态时，由 `resume_handler` 返回更新：

```python
return ResumeDecision(
    action="continue",
    state_update={"ir": confirmed_ir},
)
```

宿主必须在 state schema 中声明对应字段。before_tool 将更新提供给原工具的
`ToolRuntime.state`，工具及其 after_tool 交互完成后与结果一起提交。返回 replan/reject 时也会提交
显式提供的业务更新。若 handler 抛异常，其对 detached state 的修改不会提交。
状态合并顺序为 before_tool 更新、工具更新、after_tool 更新。普通字段采用最后一次
更新，带 reducer 的字段按声明的 reducer 依次处理；工具和后续 handler 的状态预览
遵循同样规则。提交保留原始增量，不将预览后的完整值再次送入 reducer。单次工具
涉及同一 reducer 字段的多次更新时，middleware 返回原生 Command 列表，保留更新
顺序及唯一的工具结果消息。

## IR 子代理示例与恢复范围

### 结束前的两个 guard

`hitl/completion_guards.py` 提供两个 after_model handler，已注册到 de-agent 的
`.plugin.yaml`，共用 checkpoint 中的私有 completion_guards 状态：

- `TextQuestionGuard`：声明 policy 即启用，检测最终文本最后一行的明确提问，
  将完整文本（最多 2000 字符）作为问题，每轮用户输入最多触发两次。
- `HumanFeedbackGuard`：最终确认，声明 policy 即启用，每轮用户输入只触发一次。
  策略 params.conditions 支持字符串或列表，补充最终确认清单。

de-agent 已声明这两个 policy，不需要额外的 auto_resolve 或 final_confirmation 开关。
删除对应 policy 可关闭该 guard，插件的 hitl.enabled=false 则关闭该插件全部 HITL。

三个 guard 的交互参数都放在 `.plugin.yaml` 对应 policy 的 params 中。
例如修改 TextQuestionGuard 的提问次数（下例只展示该 policy）：

```yaml
hitl:
  enabled: true
  instructions: prompts/hitl.md
  policies:
  - id: text_question_guard
    at: after_model
    handler: hitl/completion_guards.py:TextQuestionGuard
    params:
      text_question_max_fires: 2
```

policy 只负责是否启用交互，不会自动安装应答器；需调用方消费 interrupt 并回答。
总开关仍是插件声明 hitl.enabled。两个 guard 都跳过带有效或
无效工具调用的模型消息；回答后返回模型重新推理。计数在 resume 时写入，重放不会
重复累加，middleware 生成的反馈不会误判成新用户轮次。原始回答也会被 IR session
收集器按稳定 ID 收集，避免把模型引导话术当成业务事实。

真实 DataTaskIR 策略现位于 [IRQuestionGuard](../../dataagent/plugins/de-agent/hitl/ir_question_guard.py)，
策略声明见 [.plugin.yaml](../../dataagent/plugins/de-agent/.plugin.yaml) 的 hitl 字段。原 examples/hitl 的简化
ir_gate 已移除，避免把自然语言回答直接当成已确认 IR。

IRQuestionGuard 同样以 policy 声明控制启用，不再读取 enable_ir_hitl。
data_task_ir.enabled 仍是 IR 功能本身的开关；未开启 IR 时，没有口径可供检查，guard
不会触发。关键字段范围、未决问题、降级状态与提问预算等业务判断仍然保留。

de-agent 的 `.plugin.yaml` 已集中列出全部 HITL 参数及默认值：

| Policy | params 字段 | 默认值 |
| --- | --- | --- |
| text_question_guard | text_question_max_fires | 2 |
| ir_question_guard | budget | 8 |
| ir_question_guard | hitl_question_guard_max_fires | 2 |
| ir_question_guard | hitl_max_same_fingerprint_asks | 2 |
| ir_question_guard | hitl_max_same_field_asks | 2 |
| ir_question_guard | hitl_blocking_fields | identity、sources、time_context、output_features |
| ir_question_guard | consumerTools | de-agent__check_and_repair_sql；另内置识别 task 的 nl2sql 子代理 |
| human_feedback_guard | conditions | []，可选的最终确认清单 |

原 config.json 中 plugins.config.de-agent.hitl 的参数，以及 data_task_ir 下的
HITL 字段不再读取；自定义值应移到对应 policy 的 params。data_task_ir.enabled、
model 等 IR 业务参数仍保留在 config.yaml。

插件编译器自动加载 handler，使用插件 ID 作为策略命名空间。handler 可通过
state_schema 声明业务状态；middleware 自动合并这些字段，IRQuestionGuard 已声明
IRQuestionState。显式装配仍可使用 HITLMiddleware.from_config。多个插件的策略合并
为一个父级 HITLMiddleware，同名局部策略因命名空间不同而互不冲突。
默认用一条 after_model 策略覆盖消费者调用前和最终完成前，检查关键字段
unresolved + question；这与旧 guard 一样拦截整轮计划，避免并行消费者重复提问。
handler 也支持 before_tool，适用于明确保证消费者串行的宿主。回答以任务级 ir_clarification 保存，现有 IR session 收集器按
稳定 ID 去重并重新提取，不依赖 HITL 审计历史。回答后仍未决的字段不再追问，按
confirmed-only 消费并记录交付缺口。预算目前统计该任务的 IR 交互，不是跨所有策略的全局预算。

默认限制沿用旧 guard 的 2 次 fire、2 次指纹和字段上限；总 IR 预算为 8，可通过
策略 params.budget 配置。任务 ID 改变后重新计数。
- 到达每个阶段时，按配置顺序准备该阶段所有命中的策略，冻结问题、参数和策略版本。
  全部返回 continue 才沿原流程继续；后续 resume_handler 可看到前面策略的业务更新。
- 问题通过 durable prepare task 保存；SQLite 重建恢复时不重新运行 trigger/builder。
  原策略删除、阶段迁移或版本改变会拒绝恢复，不会静默放行；兼容版本由开发者手动维护。
- 恢复时 handler 可能被重放，因此必须无副作用或自行幂等。
- 仅目标工具暂停，其他并行工具仍可执行；多个等待项可以按 interrupt ID 分别恢复。
- 私有 `hitl_history` 按交互 ID 合并，通过 `graph.aget_state(config)` 查询，
  `source="external"` 表示回复从 resume 接口提交，不假定一定来自真人。
- 工具通过 durable execute task 缓存已完成结果，after_tool 等待恢复时也不会重复执行
  已完成的调用。执行中崩溃仍可能重试外部
  操作，需要业务幂等；并行业务状态更新仍需宿主自行声明合适的 reducer。
- 当前支持普通工具结果，以及本图的 `Command(update=dict)`；父图 Command、
  Command 列表和自动修改原工具参数尚未支持。

验证命令：

```bash
uv run pytest tests/test_hitl.py tests/test_hitl_stages.py -q
```
