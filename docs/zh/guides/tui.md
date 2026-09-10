# TUI 指南

这篇文档面向终端用户、远程服务器用户和开发者。当前最小版本连接 Python API，读取后端默认模型、启用的 Skill 和 MCP，通过 AG-UI 展示聊天与工具结果。

当前后端尚未开放数据源管理、历史会话恢复及旧 artifact 下载接口；TUI 根据 capability 隐藏对应命令或停止请求。`/outputs` 只展示运行事件中已有的产物。文件上传、模型/MCP 管理请使用 Web 工作台。

## 启动方式

安装好仓库依赖（Node.js 22+、uv/Python、`npm install`）后，在仓库根目录启动：

```bash
npm run start:tui
```

默认是本地模式：自动启动独立 Python 后端，建立本地身份，无需启动 Web、注册或验证邮箱。后端仅监听 `127.0.0.1` 的可用随机端口，并使用随机会话凭证及 CSRF 校验；不会复用或停止已有 GUI/API 服务。退出 TUI（本地模式下 `/logout` 也会退出）时关闭自己启动的后端。

模型配置优先级：进程环境变量 > 本地保存的模型配置 > 仓库根目录 `.env`。缺少 Provider、Model、Base URL 或 API Key 时在终端补填；密钥隐藏输入。首次初始化 Python 依赖可能需要网络，失败时会显示日志位置，不会跳转 Web 注册。

默认本地数据目录是 `~/.datafoundry/tui`，可用 `DATAFOUNDRY_TUI_HOME` 指定独立位置。其中 `model.json` 保存终端补填的模型配置，`secret` 是持久化加密/会话密钥，文件权限为仅当前用户可读写；SQLite、DataAgent 文件和日志也保存在该目录。不要删除 `secret` 后继续使用原有加密资源。本地用户和数据独立于远程/GUI 账号，不自动迁移。

显式指定后端运行入口则使用原有远程登录模式（不自动启动后端）：

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8787/api/copilotkit
```

`--resume` 仅在后端声明 `conversation.memory` 能力时可用，当前 Python 后端不支持。无需选择数据源即可开始聊天。

仅远程模式需要已有服务端账号：首次使用按服务端策略注册并完成邮箱验证，再回终端登录；登录成功缓存会话，下次恢复有效登录态。`--no-auto-login` 仅影响远程登录缓存。

查看 CLI 参数：

```bash
npm run start:tui -- --help
```

## 主界面

TUI 默认停留在 Chat。使用 `/outputs` 打开独立的全屏产出页，按 `Esc` 或 `q` 关闭。

输入区初始一行，换行或自动折行时向上扩展，最多显示六行，超出后在输入区内滚动。首页和对话页的 composer 都左对齐，随聊天区宽度伸缩，两侧各留两列空白，只保留上下分隔线。中文输入使用真实终端光标定位候选框，而不是反色字符模拟光标。满屏渲染时校正 Ink 7.1 的光标末尾定位，不再额外预留底部缓冲空行。

输入区下方紧贴显示左对齐的 `model: ...`，模型行直接贴近终端底边，数据源或连接异常合并显示在同一行右侧，不再有独立底栏。首页引导文字置于输入区上方。快捷键可通过 `/help` 查看，补全和退出确认按需提示。

执行进度显示在当前聊天轮次尾部：`Running... · 8.0s`。收到后端 `RUN_FINISHED` 并处理完缓冲内容后，留下 `✓ Run completed · 12.4s`；后端明确报错显示 `✗ Run failed`，断流显示 `Interrupted — completion unknown`。每轮独立保留耗时，从请求发起到终态事件，包含模型、工具及网络等待，不包含排队时间。

### iTerm2 换行与中文输入

TUI 启动时自动协商增强键盘协议：`Enter` 发送，`Shift+Enter` 换行；`Ctrl+J`（或 `Alt+Enter`）也可换行。

如果 iTerm2 中 `Shift+Enter` 仍被识别为发送，请检查 **Settings → Profiles → Keys → General → Apps can change how keys are reported** 是否开启，并检查 Key Mappings 中是否把 Shift+Enter 覆盖为普通回车。旧版 iTerm2 建议升级，或将 Shift+Enter 映射为 **Send Hex Codes: `0x0a`**。无需全局开启 “Report modifiers using CSI u”。如果终端只发送普通回车字节，应用无法区分 Enter 和 Shift+Enter。

候选词窗口由 iTerm2/系统输入法绘制；应用不会收到浏览器式的 composition 事件。若仍有异常，请提供 iTerm2 版本、输入法和录屏以便定位。

## Slash 命令

输入 `/` 后可以用 `Tab` 补全。当前注册的内置命令如下：

| 命令 | 作用 | 示例 |
| --- | --- | --- |
| `/help` | 查看可用命令。 | `/help` |
| `/clear` | 清空当前聊天记录。 | `/clear` |
| `/status` | 查看 thread、消息数、当前数据源和 Skill。 | `/status` |
| `/outputs` | 打开当前会话的产出页。 | `/outputs` |
| `/datasource` | 仅后端开放 `runtime.dataTools` 时可用。 | `/datasource` |
| `/skill` | 打开 Skill 选择器、列出或选择 Skill。 | `/skill show` |
| `/reset` | 创建新的本地会话。 | `/reset` |
| `/resume [latest\|list\|sessionId]` | 仅后端开放 `conversation.memory` 时可用。 | `/resume list` |
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
| `Shift+Enter` / `Ctrl+J` | 换行；Shift+Enter 需要终端支持增强键盘协议。 |

## 运行行为

登录后，TUI 从 `/api/v1/workspace-config`、`/api/v1/run-defaults`、`/api/v1/capabilities` 加载真实资源配置，替换本地旧资源 ID。自然语言输入发送到 `/api/copilotkit`，并把默认模型 ID、启用的 MCP 和 Skill 写入 `forwardedProps.run_config`。每轮只发送新增用户消息，完整历史和工具消息由后端 checkpoint 保存。后端返回 AG-UI 事件后，TUI 在 Chat 展示文本和工具调用。

只有收到 `RUN_FINISHED` 才显示完成。断流、无终态、无效事件和 `RUN_ERROR` 显示失败并保留已收到的内容；不会自动重新提交请求，避免重复执行工具。收到人工交互中断时会明确提示当前 TUI 不支持恢复，可用 `/reset` 开始新会话。

离线演示模式已移除。本地模式也运行真实 DataAgent，模型调用仍需要可用的模型服务；不要求密码账户登录。

## 典型流程

1. 执行 `npm run start:tui`，等待本地后端就绪。
2. 运行 `/status` 查看当前 thread、数据源和 Skill。
3. 可选：运行 `/skill` 选择已安装的 Skill。
4. 输入问题：

```text
请调用 list_workspace_files，列出我共享工作区中的文件，并总结结果。
```

5. 在 Chat 视图观察回复和工具调用，然后继续追问。
6. 用 `/reset` 开始新的会话，用 `/exit` 退出。

维护者可以运行 `npm run smoke:tui-local` 验证本地进程启动、自动认证和退出清理；运行 `npm run smoke:tui-python` 验证本地/远程身份、实际 Python API、DataAgent graph、真实文件工具、流式与非流式 AG-UI、Ink 渲染及第二轮 checkpoint 历史。后者使用确定性的测试模型，不验证外部模型供应商的可用性。

## 与 Web 工作台的区别

| 维度 | Web 工作台 | TUI |
| --- | --- | --- |
| 使用环境 | 浏览器、本地演示、业务分析。 | SSH、远程服务器、终端工作流。 |
| 操作方式 | 点击、输入框、控制台。 | 键盘和 slash 命令。 |
| 追溯展示 | 右侧控制台、步骤详情和追溯列表。 | Chat 记录和 `/outputs` 页面。 |
| 资源操作 | 表单创建、测试、导入和预览。 | 使用后端默认模型/MCP，选择 Skill。 |

需要完整视觉演示时，用 Web 工作台。需要在 SSH 或轻量终端环境验证 Agent 运行链路时，用 TUI。

## 排查

- 无法连接后端：确认正式态 API（`npm run start:api` 或一键部署）已在运行；贡献者热更新才用 `npm run dev:api`。
- 后端地址变更：用 `--runtime-url` 指定完整 `/api/copilotkit` 地址。
- 模型无响应：检查根目录 `.env` 中的 `LLM_PROVIDER`、`LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`。
- 会话无法恢复：当前 Python 后端没有开放历史恢复能力；同一次 TUI 会话内可以连续追问，退出后的历史恢复暂不支持。
- 命令没有效果：运行 `/help` 查看当前注册命令，再检查命令提示区的错误信息。

继续阅读：[Web 工作台指南](web-workbench.md)。
