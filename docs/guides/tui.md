# TUI

TUI 是仓库里的 `ui/tui`，与 Web 共用会话列表。没有参数时，按 `config.yaml` 拉起 `dataagent serve`，或者连上已经在跑的那一份。

## 启动

```bash
npm run start:tui
```

地址来自 Home `config.yaml` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。这个地址上已经有控制面时，同一条命令直接连上，不再启动第二个后端。`--runtime-url` 只连接、不拉起。主机可以是 Home 的 `server.host`、回环地址或其他 IP。`0.0.0.0` 和 `::` 会改走回环地址。

```bash
npm run start:tui -- --resume
npm run start:tui -- --resume <threadId>
npm run start:tui -- --workspace /existing/data
npm run start:tui -- --runtime-url http://127.0.0.1:8790
```

`--runtime-url` 不能和 `--config`、`--env-file`、`--workspace`、`--user`、`--session` 一起用。后面这几项会拉起后端。部署栈已经在跑时也可以 `./deploy.sh tui`，它连接这次 `./deploy.sh start` 记下的 API 地址。

`/exit` 会停掉这次 TUI 拉起的后端。用 `--runtime-url` 连上的控制面不会被停掉。Ctrl+C 需要按两次才退出。

## 命令

输入 `/` 打开命令，Tab 补全。可用命令：

| 命令 | 作用 |
| --- | --- |
| `/help` | 显示帮助。别名 `h`、`?` |
| `/clear` | 开始新对话。不删除已有会话 |
| `/stop` | 通过取消接口停止当前一轮，再中止本地流；保留当前会话 |
| `/resume` | 打开会话列表 |
| `/resume list` | 打开会话列表 |
| `/resume <threadId>` | 恢复该会话 |
| `/resume latest` | 恢复最近会话 |
| `/outputs` | 打开当前会话的产物列表。别名 `output` |
| `/exit` | 退出 |

Enter 发送。Shift+Enter、Alt+Enter 和 Ctrl+J 换行。直接启用 CSI-u，启动时不做探测。若 iTerm2 让 Enter 和 Shift+Enter 都发送 CR，把 Shift+Enter 绑成 ESC + `[13;2u`，或改用 Ctrl+J。

输入框从一行向上长到六行，再在内部滚动。模型页脚在 60 列和 80 列时仍留在底部。对话始终单列。

运行中继续输入的消息会排队。`NO_COLOR=1` 关闭颜色。Ctrl+Z 后再 `fg`，会恢复终端状态并重新测量光标。Ctrl+O 展开或收起工具的输入输出。

界面日志写在 `<home>/runtime/logs/tui.log`，与后端 `backend.log` 同目录。

## 你应该看到什么

- 启动后进入可输入的对话，而不是注册或配置向导
- 请求期间显示 `Running…`
- 结束后显示完成、失败或中断摘要
- `/outputs` 列出当前会话 `outputs/` 里的文件，包括恢复和重启之后的文件

摘要只存在于界面。它不写入 Checkpoint，恢复历史时也不会补出耗时。

输入法候选窗需要你在自己的终端里手工看一次。自动化 PTY 检查不能代替这一步。
