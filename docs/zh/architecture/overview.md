# 架构概览

当前产品是一个本地控制面，加上两种展示。Web 和 TUI 都连接同一个 `dataagent serve` 进程，不各自带一套运行时。

## 一个服务，两种展示

```text
浏览器 ui/web
  -> Next 同源转发 /api/v1/runs 与 /api/v1/*
  -> dataagent serve（config.yaml 的 server.host:server.port，默认 127.0.0.1:8790）
  <- ui/tui 直接请求同一进程
```

| 展示 | 启动 | 打开方式 |
| --- | --- | --- |
| Web | `npm run start` 或 `npm run start:web` | `http://127.0.0.1:3000/data-tasks` |
| TUI | `npm run start:tui` | 终端。控制面没在听就由 TUI 拉起 |

Python SDK 可以不经 HTTP，直接准备同一份运行时。Web/TUI 的对话入口是 `POST /api/v1/runs`；其他 Agent 可通过同一后端的 A2A 1.0 `POST /a2a/jsonrpc` 调用。A2A 返回 Task 和最终回答，不提供 Web/TUI 的 SSE 事件流，见 [A2A 接口](../reference/rest-api.md#a2a)。

## 主要模块

| 模块 | 职责 |
| --- | --- |
| `ui/web` | 浏览器展示。把对话和 `/api/v1/*` 转到控制面。 |
| `ui/tui` | 终端展示。连接控制面，用 slash 命令看会话和产物。 |
| `dataagent/server/` | 提供健康检查、会话和 AG-UI 事件流。A2A 服务位于 `dataagent/server/a2a/`。 |
| `dataagent/` | 提供 Python SDK。包内的 `bootstrap/` 准备运行环境，`assembly/graph.py` 装配执行图。 |
| `dataagent/assembly/prompts/` | 渲染主 Agent、子 Agent 和文件系统说明的宿主模板。插件自己的提示词放在各自的插件目录中。 |
| Home `~/.dataagent` | 配置、Skill、插件和全部会话状态。 |
| `dataagent.workspaces` | 进程启动时绑定的只读输入目录，不在 Home 里面。 |

控制面没有数据源注册、知识库、Cookie 登录或独立的配置 REST。这些能力不在当前 HTTP 接口里。

## 配置和数据放在哪

部署时要改的是 Home `~/.dataagent` 里的 `config.yaml` 和 `.env`，加上仓库根目录 `.env`。最少要填的模型字段和监听地址见 [配置](../reference/configuration-api.md)。

让模型读本机目录时，用工作区，不要在 Home 里找一份“数据源配置”。步骤见 [数据从哪里来](../guides/data-sources.md)。新产生的文件在该会话的 `outputs/`。

当前工作目录下的 `.dataagent/` 不会被读取。REST 没有访问令牌或注册接口；A2A JSON-RPC 可用 `DATAAGENT_A2A_AUTH_TOKEN` 设置共享 Bearer 令牌，它不保护 REST 接口，也不区分调用者身份。
