# Data Q&A plugin

通过现有插件机制提供两个主 Agent 工具：

- `data-qa__get_ontology_description_tool`：读取配置数据库的表、列和关联，返回 `{database, tables, columns, joins}`。
- `data-qa__request_human_feedback`：提交澄清或确认问题，使用 LangGraph `interrupt` 等待宿主恢复。

插件通过 `system_prompt_file: prompts/routing.md` 自动加载问数流程提示词：先读取 schema，再制定并更新计划，SQL 任务委派给 NL2SQL 子 Agent，最后完整返回用户要求的信息。两个插件仍独立加载，使用这套问数流程时须在主配置中显式启用 `data-qa` 和 `nl2sql`。Bio Lab 评测也使用这份插件提示词。

将以下片段合入完整 `config.yaml`：

```yaml
plugins:
  enabled:
  - data-qa
  - nl2sql
  config:
    data-qa:
      database: bio_lab
```

模型配置沿用已有 `models`。ontology 工具借用 `semantic` 配置创建的客户端，或 SDK 调用方注入的语义客户端；不创建、关闭自己的连接。`database` 在 ontology 工具调用时校验，必须是非空字符串，只来自插件配置，不作为模型填写的参数。反馈工具不需要数据库参数。

还需按数据库和模型填写 `plugins.config.nl2sql`；仅填写业务参数不会启用插件。

反馈工具的模型参数为 `question`，返回宿主提供的回答。宿主需提供 checkpointer，并使用同一 `thread_id` 和 `Command(resume=...)` 恢复；仅启用插件不会自动产生交互 UI。中断 payload 的 `kind` 为 `human_feedback`。

工具通过 `.plugin.yaml` 的 `tools` 声明导入，名称自动加 `data-qa__` 前缀。修改 manifest 可以改变本插件提供的工具；主配置没有按工具筛选插件内容的通用开关。本插件不使用独立的 `tools.enabled` 或运行时工具注册表。
