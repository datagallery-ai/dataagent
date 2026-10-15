# AgentKit

AgentKit 是本地数据 Agent 运行时。你可以用终端界面提问，用 Python SDK 接到自己的程序，或通过 HTTP 接收 AG-UI 事件流。Python 包名和命令名是 `dataagent`，代码在 `runtime/agentkit`。

运行时读取 JSON 配置和本机 Home 目录，调用已配置的模型，并按会话保存对话、产物和 Trace。插件、Skill、Hook、子代理和 MCP 用来增加工具与流程。语义服务客户端用来查询已经部署的语义服务。

可运行示例在 [examples/](../examples/README.md)。

## 从这里开始

| 你要做的事 | 先读 |
| --- | --- |
| 安装并完成第一次对话 | [快速启动](guides/quickstart.md) |
| 配置模型、限额和输入目录 | [配置](guides/configuration.md)，字段见 [配置参考](reference/configuration.md) |
| 使用终端界面 | [TUI](guides/tui.md)、[会话与产物](guides/sessions-and-outputs.md) |
| 在 Python 里调用 | [SDK](guides/sdk.md)、[Python API](reference/python-api.md) |
| 增加工具或流程 | [扩展](extensions/overview.md) |
| 查看接口和事件 | [HTTP 接口](reference/rest-api.md)、[事件](reference/events.md) |
| 了解运行过程 | [架构概览](architecture/overview.md) |
| 修改运行时并验证 | [开发环境](development/setup.md) |

## 使用

| 文档 | 内容 |
| --- | --- |
| [快速启动](guides/quickstart.md) | 安装、最小配置、第一次运行 |
| [配置](guides/configuration.md) | 配置文件、环境变量和优先级 |
| [TUI](guides/tui.md) | 启动、命令、连接已有后端 |
| [会话与产物](guides/sessions-and-outputs.md) | 恢复会话、输入目录、查看产物 |
| [SDK](guides/sdk.md) | 接入和生命周期 |
| [故障排查](guides/troubleshooting.md) | 启动、模型、连接和恢复 |

## 扩展

| 文档 | 内容 |
| --- | --- |
| [如何选择](extensions/overview.md) | Plugin、Skill、Tool、Hook、子代理和 MCP |
| [插件](extensions/plugins.md) | 目录、启用、发现和刷新 |
| [工具](extensions/tools.md) | Python 工具、模型和运行上下文 |
| [Skill](extensions/skills.md) | 编写、发现和作用域 |
| [Hook](extensions/hooks.md) | 生命周期、顺序和错误 |
| [子代理](extensions/subagents.md) | 声明、继承和执行限制 |
| [MCP](extensions/mcp.md) | 接入外部 MCP 服务 |
| [语义服务](extensions/semantic-service.md) | 客户端、认证和 TLS |

## 参考

| 文档 | 内容 |
| --- | --- |
| [配置字段](reference/configuration.md) | 字段、类型、默认值和约束 |
| [命令与环境变量](reference/cli-and-environment.md) | CLI 参数和环境变量 |
| [Python API](reference/python-api.md) | 公开 Python 接口 |
| [HTTP 接口](reference/rest-api.md) | 路径、请求、响应和错误 |
| [事件](reference/events.md) | AG-UI 事件和终止状态 |
| [扩展声明](reference/extension-schema.md) | 插件、工具、Hook、子代理 |
| [MCP 配置](reference/mcp-configuration.md) | `.mcp.json` 字段和连接 |

## 架构

| 文档 | 内容 |
| --- | --- |
| [概览](architecture/overview.md) | 分层和模块职责 |
| [启动](architecture/bootstrap.md) | 配置加载、资源发现和 Runtime |
| [执行](architecture/execution.md) | 从 HTTP 到模型与工具 |
| [上下文](architecture/context-management.md) | 摘要、长结果卸载和提示 |
| [状态与存储](architecture/state-and-storage.md) | 会话、Checkpoint 和文件 |
| [扩展生命周期](architecture/extension-lifecycle.md) | 编译、缓存和刷新 |
| [可观测性](architecture/observability.md) | 日志、Trace 和关联标识 |
| [安全](architecture/security-boundaries.md) | 身份、凭据、文件和 shell |

## 开发

| 文档 | 内容 |
| --- | --- |
| [开发环境](development/setup.md) | 依赖和调试 |
| [测试](development/testing.md) | 单元测试、集成测试和真实模型测试 |
| [内置 core](development/core-maintenance.md) | `dataagent/core` 的来源和维护 |
| [发布](development/release.md) | 构建、检查和兼容 |
