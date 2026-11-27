# de-agent 人工反馈

de-agent 可以在澄清业务口径、生成 SQL 前复核口径以及确认交付时请求人工反馈。本页介绍内置策略。要为其他插件实现策略，参见 [HITLHandler 与 HITLMiddleware](hitl.md)。

<a name="enable-de-agent-hitl"></a>

## 配置 SQL 任务

从 [de-agent 配置示例](../../../../../examples/de-agent.yaml) 开始，设置模型和语义服务，保留默认根 Agent 来编排 NL2SQL 委派。将以下设置合入产品 YAML，可开启 DataTaskIR 和可选的 SQL 生成前口径复核。

```yaml
plugins:
  enabled:
  - de-agent
  config:
    de-agent:
      data_task_ir:
        enabled: true
        enable_ir_caliber_review: true
```

两个 IR 开关默认为 `false`。插件[清单](../../../../../dataagent/plugins/de-agent/.plugin.yaml)已经启用 HITL 并提供[交互提示词](../../../../../dataagent/plugins/de-agent/prompts/hitl.md)。清单策略决定何时暂停运行。产品 YAML 决定是否运行 IR 工作流与业务口径复核。

<a name="completion-guards"></a>

## 结束前的两个 guard

两个结束策略均在 `after_model` 运行。模型响应包含有效或无效的工具调用时，这两个策略都会跳过该响应。

| 策略 | 提问时机 | 清单参数 |
| --- | --- | --- |
| `text_question_guard` | 模型文本的最后一条非空行包含符合识别规则的中文提问。取完整回复的前 2,000 个字符作为问题。 | `text_question_max_fires` 每轮用户输入默认最多 `2` 次 |
| `human_feedback_guard` | 模型准备结束时，每轮用户输入询问一次最终确认。 | `conditions` 默认为 `[]`，可填字符串或确认项列表 |

声明策略即启用对应的 guard。删除某一条策略可关闭它，在插件清单中设 `hitl.enabled: false` 可关闭 de-agent 的人工反馈。这两个策略会把回答交给模型重新规划。接受回答时更新计数。中间件生成的反馈仍属于当前用户轮次。IR 收集器按稳定 ID 收集原始回答并去重。

<a name="ir-policies"></a>

## IR 澄清与口径复核

- **`ir_question_guard`。** 启用 DataTaskIR 后，检查有明确问题的未决关键字段。默认 `after_model` 策略检查委派给 `nl2sql` 或调用指定 SQL 消费工具的计划，也检查只含文本的结束响应。它在计划中的工具执行前暂停。自建宿主如果能保证工具串行，也可以让此 guard 在 `before_tool` 执行。
- **`ir_caliber_review`。** 两个 IR 开关均启用后，在 `task(subagent_type="nl2sql")` 或 `de-agent__check_and_repair_sql` 之前，每个 IR 任务展示一次已提取的业务口径供复核。提取字段全部没有未决项时才提问。回答作为反馈交给模型重新规划。

IR 提问策略将原始回答保存到任务级 `ir_clarification`，后续由提取器重新判断字段。接受回答后仍有未决项时，任务按已确认字段继续，并在交付说明中标明缺口。切换到新的 IR 任务后重新计数。

在插件清单的 `ir_question_guard` 策略 `params` 中设置 IR 澄清上限及消费工具。

| 参数 | 默认值 | 作用范围 |
| --- | --- | --- |
| `budget` | `8` | 每个任务的 IR 澄清总预算，同时受更低生效上限约束 |
| `hitl_question_guard_max_fires` | `2` | 每个任务触发 IR 提问的次数上限 |
| `hitl_max_same_fingerprint_asks` | `2` | 同一组未决问题的触发上限 |
| `hitl_max_same_field_asks` | `2` | 单字段上限。已有回答记录的字段会先由 guard 跳过 |
| `hitl_blocking_fields` | `identity`、`sources`、`time_context`、`output_features` | 可触发澄清的字段 |
| `consumerTools` | `de-agent__check_and_repair_sql` | 执行 SQL 工作前需要检查的工具。委派给 `nl2sql` 单独识别 |

预算只适用于当前任务的 IR 澄清。结束策略按各自的单轮上限计数。

<a name="migrate-legacy-settings"></a>

## 迁移旧版配置

用清单中的策略声明接管旧 `enable_human_feedback` 和 `enable_ir_hitl` 对应的行为。将原来放在 `plugins.config.de-agent.hitl` 或旧 IR HITL 字段中的自定义上限移到相应策略的 `params`。`data_task_ir.enabled`、`data_task_ir.model` 和 `data_task_ir.enable_ir_caliber_review` 等业务设置继续放在产品 YAML。

最终确认清单放进对应策略的 `conditions`。原 `human_feedback_conditions` 或 Initial/Mid/Final 阶段中面向模型的说明，放进 `hitl.instructions` 指向的 Markdown。当前由 Web、TUI 或原集成入口提交回答。旧 `hitl_auto_resolve` 没有对应开关。

<a name="verify-de-agent-hitl"></a>

## 验证场景

1. 使用 de-agent 配置开启新会话。提交一条生成 SQL 的任务，选用同时允许 `user_id` 和 `device_id` 的源表，暂不指定去重主键。
2. 会话暂停时回答正确的主键，如果提示需要源表则一并回答。确认同一会话继续运行。启用口径复核且 IR 字段均已明确时，确认本任务生成 SQL 前只提示复核一次。
3. 核对 SQL 和交付说明是否采用所选主键，并说明剩余缺口。最终确认已启用时回答该问题。也验证一次回答后字段依然无法确认的情况。

[结束策略测试](../../../../../tests/test_de_agent_completion_guards.py)、[IR 提问测试](../../../../../tests/test_de_agent_ir_question_guard.py)和[口径复核测试](../../../../../tests/test_de_agent_delivery_guards.py)使用本地测试夹具。可从仓库根目录一起运行。

```bash
uv run --no-sync pytest -q tests/test_de_agent_completion_guards.py \
  tests/test_de_agent_ir_question_guard.py tests/test_de_agent_delivery_guards.py
```
