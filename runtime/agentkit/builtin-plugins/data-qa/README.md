# Data Q&A plugin

通过现有插件机制提供两个主 Agent 工具：

- `data-qa__get_ontology_description_tool`：读取配置数据库的表、列和关联，返回 `{database, tables, columns, joins}`。
- `data-qa__request_human_feedback`：提交澄清或确认问题，使用 LangGraph `interrupt` 等待宿主恢复。

插件没有 system prompt、skills 或子 Agent，也不依赖 NL2SQL。它不会自行决定数据库任务的执行流程；需要这些流程的宿主负责提供指令。Bio Lab 评测仍由测试入口读取自己的 `prompt.md` 并注入 system 消息。

将以下片段合入完整 `config.yaml`：

```yaml
plugins:
  enabled:
  - data-qa
  config:
    data-qa:
      database: bio_lab
```

模型配置沿用已有 `models`。ontology 工具借用 `semantic` 配置创建的客户端，或 SDK 调用方注入的语义客户端；不创建、关闭自己的连接。`database` 在 ontology 工具调用时校验，必须是非空字符串，只来自插件配置，不作为模型填写的参数。反馈工具不需要数据库参数。

需要 NL2SQL 子 Agent 时可同时启用 `"nl2sql"`，并使用原有 `plugins.config.nl2sql` 配置；两个插件独立加载。

反馈工具的模型参数为 `question`，返回宿主提供的回答。宿主需提供 checkpointer，并使用同一 `thread_id` 和 `Command(resume=...)` 恢复；仅启用插件不会自动产生交互 UI。中断 payload 的 `kind` 为 `human_feedback`。

工具通过 `.plugin.yaml` 的 `tools` 声明导入，名称自动加 `data-qa__` 前缀。修改 manifest 可以改变本插件提供的工具；主配置没有按工具筛选插件内容的通用开关。本插件不使用独立的 `tools.enabled` 或运行时工具注册表。
