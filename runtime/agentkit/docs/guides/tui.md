# TUI

TUI 是仓库里的 `apps/tui`。`--v2` 让它启动并管理 AgentKit 后端。运行时目录里没有另一套终端界面。

## 启动自己的后端

在仓库根目录：

```bash
uv sync --project runtime/agentkit --locked
npm run start:tui -- --v2
```

常用参数：

```bash
npm run start:tui -- --v2 --env-file .env --workspace "$PWD"
npm run start:tui -- --v2 --config runtime/agentkit/examples/config.json
npm run start:tui -- --v2 --resume
npm run start:tui -- --v2 --resume <threadId>
```

相对路径以启动 npm 的目录为基准。这个目录只解析路径，不发现配置或扩展。

`/exit` 关闭界面，并停止由它启动的后端。Ctrl+C 需要按两次才退出。

## 连接已经运行的后端

```bash
npm run start:tui -- --v2 --runtime-url http://127.0.0.1:8790/dataagent/stream
```

这个模式不登录，也不带令牌。TUI 不拥有该服务，退出时不会停止它。不要同时使用 `--config`、`--env-file` 或 `--workspace`。

先单独启动后端：

```bash
uv run --project runtime/agentkit dataagent serve
```

## 命令

输入 `/` 打开命令，Tab 补全。V2 提供：

| 命令 | 作用 |
| --- | --- |
| `/help` | 显示帮助。别名 `h`、`?` |
| `/clear` | 开始新对话。不删除已有会话 |
| `/resume` | 打开会话列表 |
| `/resume <threadId>` | 恢复该会话 |
| `/resume latest` | 恢复最近会话 |
| `/outputs` | 打开当前会话的产物列表。别名 `output` |
| `/exit` | 退出 |

Enter 发送。Shift+Enter、Alt+Enter 和 Ctrl+J 换行。V2 直接启用 CSI-u，启动时不做探测。若 iTerm2 让 Enter 和 Shift+Enter 都发送 CR，把 Shift+Enter 绑成 ESC + `[13;2u`，或改用 Ctrl+J。

输入框从一行向上长到六行，再在内部滚动。模型页脚在 60 列和 80 列时仍留在底部。对话始终单列。

运行中继续输入的消息会排队。`NO_COLOR=1` 关闭颜色。Ctrl+Z 后再 `fg`，会恢复终端状态并重新测量光标。Ctrl+O 展开或收起工具的输入输出。

日志路径 `DATAAGENT_V2_LOG_PATH` 必须设在 TUI 进程环境里。

## 你应该看到什么

- 启动后进入可输入的对话，而不是注册或配置向导
- 请求期间显示 `Running…`
- 结束后显示完成、失败或中断摘要
- `/outputs` 列出当前会话 `outputs/` 里的文件，包括恢复和重启之后的文件

摘要只存在于界面。它不写入 Checkpoint，恢复历史时也不会补出耗时。

输入法候选窗需要你在自己的终端里手工看一次。自动化 PTY 检查不能代替这一步。
