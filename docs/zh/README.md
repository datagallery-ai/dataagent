# 中文文档

这是产品说明：怎么部署，怎么用网页和 TUI，对外 HTTP 长什么样。运行时的字段表、插件和 SDK 不在这套文档里。

产品是一个本地控制面，加上 Web 和 TUI 两种展示。两边连接同一个 `dataagent serve`。中文和英文各有一份。

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
| 把本机目录交给模型 | [数据从哪里来](guides/data-sources.md) |
| 查看能配哪些输入 | [支持的输入](reference/supported-datasources.md) |
| 了解 API 和配置 | [REST API](reference/rest-api.md)、[配置](reference/configuration-api.md)、[Agent Runtime](reference/agent-runtime.md) |
| 了解系统结构 | [架构概览](architecture/overview.md) |
| 检查安全边界 | [安全说明](security.md) |

## 推荐体验路径

第一次试用建议按下面的顺序阅读：

1. 阅读 [产品概览](overview.md)，确认它是一个控制面和两种展示。
2. 按 [快速开始](quick-start.md) 写入模型密钥，再在网页或 TUI 里提问。
3. 查看 [能力全览](capabilities.md)，区分控制面真正提供的接口和页面上残留的旧入口。
4. 根据使用入口选择 [Web 工作台指南](guides/web-workbench.md) 或 [TUI 指南](guides/tui.md)。
5. 需要让模型读本机目录时，阅读 [数据从哪里来](guides/data-sources.md)。

## 文档边界

试用从 `overview.md`、`quick-start.md`、`capabilities.md` 和 `guides/` 开始。要接 HTTP，再读 `reference/`。这里不写插件声明、Hook 或启动内部顺序。
