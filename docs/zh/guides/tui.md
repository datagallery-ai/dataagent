# TUI 指南

这篇文档面向终端用户、远程服务器用户和开发者。读完后，你可以启动 TUI、进入对话、恢复会话，并在终端里查看运行过程和产出。

## 启动方式

已安装 TUI npm 包时，用 `dataagent-cli --runtime-url <Server 地址>` 或 `da --runtime-url <Server 地址>` 连接已启动的 Server。安装步骤见 [DataAgent 包部署](deploy-packages.md#connect-tui)。

从源码启动时，TUI 按 `config.yaml` 连接 `/api/v1/runs`。该地址上没有控制面时会拉起 `dataagent serve`。

```bash
npm run start:tui
```

地址来自 Home `config.yaml` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。这个地址上已经有控制面时，同一条命令直接连上，不再启动第二个后端。`npm run start -- --config` 绑到另一份文件的端口时，不带参数的 TUI 不会跟着走；Home 那个地址空着，就会按 Home 再起一个后端。要连已经起来的那一个，用 `--runtime-url`。`--runtime-url` 只连接、不拉起。主机可以是 Home 的 `server.host`、回环地址或其他 IP。`0.0.0.0` 和 `::` 会改走回环地址。

`./deploy.sh tui` 连接已有后端，地址优先取 `--runtime-url`，其次是最近一次托管启动记录的 API 地址，再其次是 Home 配置。例如 `./deploy.sh tui --runtime-url http://127.0.0.1:8791`。

恢复最近的服务端会话：

```bash
npm run start:tui -- --resume
```

恢复指定 thread/session：

```bash
npm run start:tui -- --resume thread-001
```

从源码启动时，只要传入 `--config`、`--env-file`、`--user` 或 `--session`，TUI 就会启动一个新的后端进程，并将这些参数传给它。显式传入 `--config` 时，后端使用指定的 YAML 文件代替 Home `config.yaml`。这些参数不能与 `--runtime-url` 同时使用。

源码 TUI 拉起后端时，会把仓库根目录的 `.env` 加载到进程环境。终端已有的同名变量优先，随后由进程环境中的非空值覆盖后端所选环境文件里的值。后端默认选择 Home `.env`，也可以用 `--env-file` 指定。通过 `--runtime-url` 连接时，使用已有后端的配置。

退出 TUI 会停止它自己拉起的后端。连接前已经运行的后端会继续运行。

查看 CLI 参数：

```bash
npm run start:tui -- --help
```

## 主界面

TUI 默认停留在 Chat。使用 `/outputs` 可以像 `/resume` 一样打开独立的全屏产出页，按 `Esc` 或 `q` 关闭。输入框最多显示六行，超过后在框内滚动。

## Slash 命令

输入 `/` 后可以用 `Tab` 补全。当前注册的内置命令如下：

| 命令 | 作用 | 示例 |
| --- | --- | --- |
| `/help` | 查看可用命令。别名为 `h` 和 `?`。 | `/help` |
| `/clear` | 开始新对话。已保存的会话仍留在磁盘上。 | `/clear` |
| `/stop` | 停止当前这一轮。调用 `POST /api/v1/runs/{runId}/cancel`，然后断开本地流。会话留在原线程。 | `/stop` |
| `/outputs` | 打开当前会话的产出页。别名为 `output`。 | `/outputs` |
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
| `Shift+Enter`、`Alt+Enter` 或 `Ctrl+J` | 插入换行。 |
| `Ctrl+O` | 展开或收起工具的输入和输出详情。 |
| `Ctrl+Z` 后执行 `fg`（Linux 和 macOS） | 挂起并恢复 TUI，恢复后会重新定位输入光标。 |

## 运行行为

TUI 把自然语言输入发到控制面的 `/api/v1/runs`，与 Web 共用会话列表。会话产出可通过 `/outputs` 查看。

`/resume` 向流地址所在源（`http://<server.host>:<server.port>`）发送 `GET /api/v1/sessions`，不带登录凭证。恢复留在服务返回的 `threadId` 上。`/stop` 使用和网页相同的取消接口。

运行期间发送的新消息会排队。聊天界面在等待响应流时显示 `Running…`，结束后显示完成、失败或中断摘要。摘要耗时不含排队时间，只显示在当次运行的 TUI 中。恢复会话时不会补算历史耗时。

## 典型流程

1. 按上面的方式启动 TUI 并连接后端。
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
| 配置 | 不能在页面上改。两边都用控制面启动时的 `config.yaml`。 | 同左。`/resume` 恢复会话，`/outputs` 查看产物。 |

需要完整视觉演示时，用 Web 工作台。需要在 SSH 或轻量终端环境验证 Agent 运行链路时，用 TUI。

## 排查

- 无法连接：直接 `npm run start:tui`。它连 Home `config.yaml` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`；那里没有控制面时会拉起一个。已经用另一份 `--config` 启动的控制面，用 `--runtime-url` 连接。`./deploy.sh` 管的栈用 `./deploy.sh tui`，它走启动时记下的 API 地址。
- 只想连接、不要拉起：用 `--runtime-url`。主机可以是 Home 的 `server.host`、回环地址或其他 IP。`0.0.0.0` 和 `::` 会改走回环地址。
- 模型无响应：检查正在运行的后端所用进程环境或环境文件里的 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`。
- iTerm2 中 `Shift+Enter` 触发发送：绑定为「Send Escape Sequence」，填入 `[13;2u`，或改用 `Ctrl+J`。输入法候选窗需在实际使用的终端中检查。
- 颜色和连接诊断：在 TUI 进程环境中设置 `NO_COLOR=1` 可关闭颜色。界面日志在客户端 DataAgent Home 的 `runtime/logs/tui.log`。
- 会话无法恢复：确认流地址所在源（`http://<server.host>:<server.port>`）上的 `GET /api/v1/sessions` 可访问。TUI 调用该接口时不带登录凭证。
- 命令没有效果：运行 `/help` 查看当前注册命令，再检查命令提示区的错误信息。

继续阅读：[Web 工作台指南](web.md)。
