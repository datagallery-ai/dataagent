# 架构概览

DataAgent 运行一个本地控制面。Web 工作台和终端客户端共用对话运行时和会话数据。

## 客户端如何连接

| 客户端 | 连接方式 |
| --- | --- |
| Web | Next.js 将同源的 `/api/v1/*` 请求转发给控制面。 |
| TUI | 直接连接控制面。源码版 TUI 可以按需拉起本地后端。 |
| Python SDK | 在调用方的 Python 进程内准备运行时并调用 Agent。 |
| 远端 Agent | 通过 Agent Card 发现服务，使用 A2A JSON-RPC 发送消息和查询 Task。 |

Web 和 TUI 都向 `POST /api/v1/runs` 提交对话请求并消费 AG-UI 事件流。A2A 在同一个控制面上提供独立的 Task 协议。[REST API](../reference/rest-api.md) · [AG-UI 接入](../reference/agent-runtime.md) · [A2A 接口](../reference/a2a.md) · [Python SDK](../guides/integrate/sdk.md)

## 配置和数据放在哪

控制面从配置读取模型、插件和默认输入目录。DataAgent Home 保存本地配置、扩展和运行状态。新会话绑定输入目录和产物位置，恢复时沿用原绑定。产物默认存放在 Home 下，也可指定其他位置。[配置](../reference/configuration-api.md) · [输入目录](../guides/inputs.md) · [会话与产物](../guides/sessions-and-outputs.md)

网络访问、凭据和文件权限见[安全说明](security.md)。源码模块和装配顺序见[开发者架构](../../development/architecture/overview.md)。
