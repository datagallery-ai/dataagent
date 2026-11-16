# TUI 指南

这篇文档面向终端用户、远程服务器用户和开发者。读完后，你可以启动 TUI、进入对话、恢复会话，并在终端里查看运行过程和产出。

## 启动方式

直接启动时，TUI 按 `config.json` 连接 `/api/v1/runs`：没有控制面就拉起 `dataagent serve`，已经在跑就连上。旧的登录和 CopilotKit 客户端已经删除。

```bash
npm run start:tui
```

地址来自 Home `config.json` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。这个地址上已经有控制面时，同一条命令直接连上，不再启动第二个后端。`npm run start -- --config` 绑到另一份文件的端口时，不带参数的 TUI 不会跟着走；Home 那个地址空着，就会按 Home 再起一个后端。要连已经起来的那一个，用 `--runtime-url`。`./deploy.sh tui` 连接这次 `./deploy.sh start` 记下的 API 地址。`--runtime-url` 只连接、不拉起。主机可以是 Home 的 `server.host`、回环地址或其他 IP。`0.0.0.0` 和 `::` 会改走回环地址。

恢复最近的服务端会话：

```bash
npm run start:tui -- --resume
```

恢复指定 thread/session：

```bash
npm run start:tui -- --resume thread-001
```

`--config` 用那份文件代替 Home `config.json`，并拉起后端，不附着已经在跑的控制面。`--env-file`、`--workspace`、`--user`、`--session` 同样传给这次拉起的后端。

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
| `/clear` | 开始新对话。已保存的会话仍留在磁盘上。 | `/clear` |
| `/stop` | 停止当前这一轮。调用 `POST /api/v1/runs/{runId}/cancel`，然后断开本地流。会话留在原线程。 | `/stop` |
| `/outputs` | 打开当前会话的产出页。 | `/outputs` |
| `/resume` | 打开会话列表。 | `/resume` |
| `/resume list` | 打开会话列表。 | `/resume list` |
| `/resume latest` | 恢复最近的服务端会话。 | `/resume latest` |
| `/resume <id>` | 恢复该会话。 | `/resume thread-001` |
| `/exit` | 退出 TUI。 | `/exit` |

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

TUI 把自然语言输入发到控制面的 `/api/v1/runs`，与 Web 共用会话列表。会话产出可通过 `/outputs` 查看。

`/resume` 向流地址所在源（`http://<server.host>:<server.port>`）发送 `GET /api/v1/sessions`，不带登录凭证。恢复留在服务返回的 `threadId` 上。`/stop` 使用和网页相同的取消接口。

## 典型流程

1. 运行 `npm run start:tui`。控制面没起时由 TUI 拉起；已经在 `config.json` 的地址上时直接连上，默认 `http://127.0.0.1:8790`。
2. 输入问题：

```text
用三句话说明你现在能做什么。
```

3. 在 Chat 视图观察流式回复和工具调用。
4. 运行 `/outputs` 查看产出。
5. 运行 `/resume` 或 `/resume list` 打开会话列表。`/resume latest` 恢复最近会话，`/resume <id>` 恢复指定会话。

## 与 Web 工作台的区别

| 维度 | Web 工作台 | TUI |
| --- | --- | --- |
| 使用环境 | 浏览器、本地演示、业务分析。 | SSH、远程服务器、终端工作流。 |
| 操作方式 | 点击、输入框、控制台。 | 键盘和 slash 命令。 |
| 追溯展示 | 右侧控制台、步骤详情和追溯列表。 | Chat 记录和 `/outputs` 页面。 |
| 配置 | 不能在页面上改。两边都用控制面启动时的 `config.json`。 | 同左。`/resume` 恢复会话，`/outputs` 查看产物。 |

需要完整视觉演示时，用 Web 工作台。需要在 SSH 或轻量终端环境验证 Agent 运行链路时，用 TUI。

## 排查

- 无法连接：直接 `npm run start:tui`。它连 Home `config.json` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`；那里没有控制面时会拉起一个。已经用另一份 `--config` 启动的控制面，用 `--runtime-url` 连接。`./deploy.sh` 管的栈用 `./deploy.sh tui`，它走启动时记下的 API 地址。
- 只想连接、不要拉起：用 `--runtime-url`。主机可以是 Home 的 `server.host`、回环地址或其他 IP。`0.0.0.0` 和 `::` 会改走回环地址。
- 模型无响应：检查根目录 `.env` 中的 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`。
- 会话无法恢复：确认流地址所在源（`http://<server.host>:<server.port>`）上的 `GET /api/v1/sessions` 可访问。TUI 调用该接口时不带登录凭证。
- 命令没有效果：运行 `/help` 查看当前注册命令，再检查命令提示区的错误信息。

继续阅读：[Web 工作台指南](web-workbench.md)。
