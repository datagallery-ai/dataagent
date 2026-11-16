# 业务澄清（HITL）

迁移来源为 main 的 !453、!571、!574；IR 多轮上下文复用 !577 的原生迁移。
不再使用 Flex HumanFeedbackNode、GlobalStateProxy 或 ActionNode。

## 配置

默认关闭。配置写在当前 Agent 的 `plugins.config.de-agent.hitl`：

```json
{
  "enabled": true,
  "auto_resolve": false,
  "budget": 4,
  "model": "primary",
  "final_confirmation": false,
  "human_feedback_conditions": ["统计粒度无法从用户原话和元数据确定时，必须确认"]
}
```

代码缺省预算为 4；旧版示例中的 8 是显式配置，可以用 `budget: 8` 保持。
关闭时不向模型暴露 `de-agent__request_human_feedback`。开启后注入
`prompts/hitl.md` 中迁移的分阶段提问纪律、触发条件和剩余预算。
`human_feedback_conditions` 可追加条件；`final_confirmation` 选择是否在最终回复前强制确认一次。

## 真实用户回答

工具参数为 `reason` 和 `pending_action`。问题必须给出具体倾向方案与候选项。
工具暂停在 LangGraph interrupt；会话状态为 `waiting_for_input`。
Web/TUI 在现有输入框输入回答即可，服务端把该文本绑定到此会话唯一的待处理问题，
恢复原 checkpoint。多问题并行时必须使用显式 resume，不能自动猜测对应关系。

`GET /sessions/{threadId}` 返回问题消息和 `pendingInterrupts`，刷新或重启后仍可回答。
也可向 `POST /dataagent/stream` 发送原生协议：

```json
{
  "threadId": "existing-thread",
  "runId": "new-run-id",
  "messages": [],
  "resume": [{"interruptId": "pending-id", "status": "resolved", "payload": "使用 user_id"}]
}
```

服务端校验用户、会话、待处理 interrupt、非空回答及重放；不接受客户端注入 state。
同一回答只产生一个 ToolMessage、只计数一次。取消或系统兜底不作为用户确认写入 IR。
工具审批仍是独立的原生 `HumanInTheLoopMiddleware`；此接口仅开放业务澄清的恢复。

## 无人值守评测

设置 `auto_resolve: true`，并在每题受信任配置中提供：

```json
{
  "enabled": true,
  "auto_resolve": true,
  "budget": 8,
  "golden_sql_path": "/absolute/path/task.sql",
  "eval_task_name": "task-1",
  "model": "primary",
  "timeout_seconds": 60
}
```

精确 golden 路径优先；也支持旧版 `task_index_path` 的索引匹配。
原有分类器、程序化答复、LLM 兜底、错误确认校验及防表名泄漏规则保留。
模型兜底使用原生流式调用，取消或超时会取消该模型调用。
golden 路径和 SQL 不通过用户输入、主 Agent 的提示词或工具结果传递。
回答标记为 `human`、`evaluation` 或 `policy`；自动兜底不能冒充人工确认。

预算、历史、去重与熔断状态通过原生 middleware.state_schema 注册并写入 checkpoint。
开启多轮 IR 时，新任务重置预算，继续任务继承；关闭 IR 时，同一 thread 默认属于同一任务，
评测应每题使用独立 thread，也可显式配置 `hitl.task_id`。
评测确认/拒绝台账和扩池标记放在输出目录 `.hitl/<task-key>/`，不同任务隔离。

## IR 联动

配置在 `plugins.config.de-agent.data_task_ir`；需要同时启用 HITL 和 IR：

| 参数 | 缺省值 |
| --- | --- |
| `enable_ir_hitl` | `true` |
| `hitl_blocking_fields` | `identity`、`sources`、`time_context`、`output_features` |
| `hitl_max_same_fingerprint_asks` | `2` |
| `hitl_max_same_field_asks` | `2` |
| `hitl_question_guard_max_fires` | `2` |

仅关键字段同时存在 `unresolved` 与非空 `question` 时入队。after_model 在 NL2SQL/
checker 执行前改写为提问；before_tool 再检查一次。回答后刷新 IR；仍未解决的已答字段、
重复问题或预算/触发次数耗尽时，按旧版策略降级为仅使用已确认约束，并说明未决项。
暂停前的任务和证据边界保存在 checkpoint，重启恢复不会把新回答误当成历史反馈。
`enable_ir_hitl: false` 关闭自动澄清和消费者拦截，保留普通 IR 刷新。
