# TUI 指南

这篇文档面向终端用户、远程服务器用户和开发者。读完后，你可以启动 TUI、连接后端、选择数据源或 Skill、恢复服务端会话，并在终端里查看运行过程和产出。

## 启动方式

先启动完整开发服务或后端：

```bash
npm run dev
```

启动 TUI：

```bash
npm run start:tui
```

指定后端运行入口：

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8787/api/copilotkit
```

指定默认数据源和 Agent 名称：

```bash
npm run start:tui -- --datasource-id dtc-growth-demo --agent dataFoundry
```

恢复最近的服务端会话：

```bash
npm run start:tui -- --resume
```

恢复指定 thread/session：

```bash
npm run start:tui -- --resume thread-001
```

TUI 需要连接正在运行的 API，并用密码账户登录（离线演示模式已移除）：

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8787/api/copilotkit
```

查看 CLI 参数：

```bash
npm run start:tui -- --help
```

## 主界面

TUI 默认停留在 Chat。使用 `/outputs` 可以像 `/resume` 一样打开独立的全屏产出页，按 `Esc` 或 `q` 关闭。

## Slash 命令

输入 `/` 后可以用 `Tab` 补全。当前注册的内置命令如下：

| 命令 | 作用 | 示例 |
| --- | --- | --- |
| `/help` | 查看可用命令。 | `/help` |
| `/clear` | 清空当前聊天记录。 | `/clear` |
| `/status` | 查看 thread、消息数、当前数据源和 Skill。 | `/status` |
| `/outputs` | 打开当前会话的产出页。 | `/outputs` |
| `/datasource` | 打开数据源选择器。 | `/datasource` |
| `/skill` | 打开 Skill 选择器、列出或选择 Skill。 | `/skill show` |
| `/reset` | 创建新的本地会话。 | `/reset` |
| `/resume [latest\|list\|sessionId]` | 恢复服务端历史会话。 | `/resume list` |
| `/exit` | 退出 TUI。 | `/exit` |

`/datasource` 支持这些用法：

```text
/datasource
```

`/skill` 支持这些用法：

```text
/skill
/skill show
/skill current
/skill select <id>
/skill <id>
/<skill-id>
```

## 快捷键

| 快捷键 | 功能 |
| --- | --- |
| `Ctrl+C` | 清空当前输入；1 秒内再次按下退出程序。 |
| `Ctrl+L` | 清空聊天显示。 |
| `Ctrl+N` | 创建新会话。 |
| `PageUp` / `PageDown` | 在 Chat 视图滚动。 |
| `Home` / `End` | 跳到 Chat 滚动区顶部或底部。 |
| 终端粘贴快捷键 | 粘贴文本；超过 1000 个字符或 10 行的内容会在输入框中折叠，发送时自动展开。 |
| `Tab` | 在输入框内补全命令。 |
| `↑` / `↓` | 先在多行输入中移动，再吸附到行首/行尾后浏览历史；原草稿和历史均会保留。 |
| `Ctrl+U` | 清空当前输入。 |
| `Ctrl+W` | 删除当前输入里的前一个词。 |
| `Enter` | 发送消息或执行命令。 |

## 运行行为

连接真实后端时，TUI 会把自然语言输入发送到 `/api/copilotkit`，并把当前数据源、启用资源和 Skill 选择写入 `run_config`。后端返回 AG-UI 事件后，TUI 会在 Chat 展示文本和工具调用；会话产出可通过 `/outputs` 查看。

`/resume` 依赖 `/api/v1/sessions` 和 `/api/v1/sessions/:id/conversation`。后端不可用或服务端不支持会话接口时，TUI 会在命令提示区显示错误。

离线演示模式已移除。TUI 始终需要可用的 API 与密码账户登录。

## 典型流程

1. 启动后端和 TUI。
2. 运行 `/status` 查看当前 thread、数据源和 Skill。
3. 运行 `/datasource` 打开数据源选择器。
4. 需要指定数据源时，在选择器里选中 `dtc-growth-demo` 并按 Enter。
5. 输入问题：

```text
帮我查看当前数据源有哪些表，并统计 orders 表各渠道 GMV。
```

6. 在 Chat 视图观察流式回复和工具调用。
7. 运行 `/outputs` 查看产出。

## 与 Web 工作台的区别

| 维度 | Web 工作台 | TUI |
| --- | --- | --- |
| 使用环境 | 浏览器、本地演示、业务分析。 | SSH、远程服务器、终端工作流。 |
| 操作方式 | 点击、输入框、控制台。 | 键盘和 slash 命令。 |
| 追溯展示 | 右侧控制台、步骤详情和追溯列表。 | Chat 记录和 `/outputs` 页面。 |
| 资源操作 | 表单创建、测试、导入和预览。 | 选择数据源和 Skill，查看配置状态。 |

需要完整视觉演示时，用 Web 工作台。需要在 SSH 或轻量终端环境验证 Agent 运行链路时，用 TUI。

## 排查

- 无法连接后端：确认 `npm run dev` 或 `npm run dev:api` 正在运行。
- 后端地址变更：用 `--runtime-url` 指定完整 `/api/copilotkit` 地址。
- 模型无响应：检查根目录 `.env` 中的 `LLM_PROVIDER`、`LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`。
- 会话无法恢复：确认后端 `/api/v1/sessions` 接口可访问，并使用真实后端模式启动。
- 命令没有效果：运行 `/help` 查看当前注册命令，再检查命令提示区的错误信息。

继续阅读：[Web 工作台指南](web-workbench.md)。
