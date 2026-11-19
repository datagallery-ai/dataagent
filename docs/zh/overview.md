# 产品概览

这是一个本地控制面，外加两种展示：浏览器里的 Web，和终端里的 TUI。两边连的是同一个 `dataagent serve`。

## 你可以用它做什么

配好模型之后，在网页或终端里用自然语言提问。运行时按 Home `config.yaml` 装配模型、插件、Skill 和 MCP，把这一轮的回复流式送回来，并把会话和产物留在 `~/.dataagent`。

如果配置了只读工作区，模型可以读那些目录里的文件。新文件写在该会话的 `outputs/`。

## 使用入口

| 入口 | 适合 | 说明 |
| --- | --- | --- |
| Web | 浏览器里看对话和会话 | `http://127.0.0.1:3000/data-tasks`。没有注册登录。 |
| TUI | SSH 和终端 | `npm run start:tui`。控制面没起时由它拉起。 |

后端地址默认是 `http://127.0.0.1:8790`。集成方直接调用这个控制面，不必再走网页。

## 边界

- 配置在文件里，不在网页表单里。
- 没有数据源注册接口，也没有知识库接口。
- 工作区是启动时绑定的只读目录，不能在对话请求里临时改。
- 默认只听本机。REST 不校验令牌；A2A JSON-RPC 可配置共享 Bearer 令牌，令牌不保护 REST。
- 数据库类问答取决于是否启用了相应插件，不是控制面自带的能力。

## 下一步

- 本地跑起来：[快速开始](quick-start.md)
- 两种展示分别能做什么：[能力全览](capabilities.md)
- 浏览器：[Web 工作台指南](guides/web-workbench.md)
- 终端：[TUI 指南](guides/tui.md)
- 配置和工作区：[配置](reference/configuration-api.md)
- 接口：[REST API](reference/rest-api.md)、[Agent Runtime](reference/agent-runtime.md)
- 结构：[架构概览](architecture/overview.md)
