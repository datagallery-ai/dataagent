# DataFoundry 中文文档

DataFoundry 是一个面向数据分析场景的 AI 工作台。它把自然语言提问、数据源管理、只读 SQL 执行、分析追溯和结果产出放在同一个工作流里，帮助用户更快完成探索性数据分析。

这套文档面向产品试用、客户演示、开源访客和集成开发者。公开文档提供中文与英文两个版本。

- English: [English documentation](../en/README.md)
- 中文：当前页面

## 从这里开始

| 你想做什么 | 推荐阅读 |
| --- | --- |
| 了解产品定位和适用场景 | [产品概览](overview.md) |
| 本地跑通一次演示 | [快速开始](quick-start.md) |
| 查看 Web、TUI、API 分别支持什么 | [能力全览](capabilities.md) |
| 使用图形化工作台 | [Web 工作台指南](guides/web-workbench.md) |
| 使用终端界面 | [TUI 指南](guides/tui.md) |
| 连接自己的数据源 | [数据源指南](guides/data-sources.md) |
| 查看支持的数据源 | [支持的数据源](reference/supported-datasources.md) |
| 了解 API 和集成方式 | [REST API 参考](reference/rest-api.md)、[配置 API 参考](reference/configuration-api.md) 与 [Agent Runtime 参考](reference/agent-runtime.md) |
| 了解系统结构 | [架构概览](architecture/overview.md) |
| 检查安全边界 | [安全说明](security.md) |

## 推荐体验路径

第一次试用建议按下面的顺序阅读：

1. 阅读 [产品概览](overview.md)，确认它解决的问题和能力边界。
2. 按 [快速开始](quick-start.md) 配置模型 API Key，并使用内置 DTC Growth Review 数据源跑通第一个问题。
3. 查看 [能力全览](capabilities.md)，了解 Web 工作台、TUI 和后端 API 的能力覆盖。
4. 根据使用入口选择 [Web 工作台指南](guides/web-workbench.md) 或 [TUI 指南](guides/tui.md)。
5. 需要接入自有数据时，再阅读 [数据源指南](guides/data-sources.md)。

## 文档边界

本目录聚焦对外阅读体验，不包含项目管理信息、实现计划、AI 协作记录、历史重构日志或来源敏感的早期讨论。公开文档只描述当前代码、配置、脚本和本地 README 能确认的能力。

如果你是二次开发或集成方，请优先阅读 `reference/` 和 `architecture/`；如果你只是试用产品，`overview.md`、`quick-start.md`、`capabilities.md` 和 `guides/` 已覆盖主要路径。
