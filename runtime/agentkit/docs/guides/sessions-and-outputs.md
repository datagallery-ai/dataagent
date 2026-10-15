# 会话与产物

一次对话对应一个 `threadId`。消息正文在 LangGraph Checkpoint 里。会话索引、输入绑定和产物在 Home 的运行目录里。

## 目录

```text
<home>/runtime/
  checkpoints.sqlite
  sessions.sqlite
  backend.lock
  logs/backend.log
  users/<user>/sessions/<thread>/
    session.json
    outputs/
    logs/
    traces/<run_id>.json
```

默认 user 是 `default`。`--user` 选择本地资源目录，不是登录。`threadId` 和 user id 只能包含字母、数字、下划线和连字符，最长 128 个字符。

## 输入目录

输入来源由高到低：

1. 重复的 `--workspace`
2. 环境变量 `DATAAGENT_WORKSPACE`
3. 配置 `dataagent.workspaces`

都没有时，列表为空。显式路径必须已经存在，并且进程可以读取和进入。

新会话把当时的输入列表写入 `session.json`，之后不再改写。恢复会话时继续使用这份绑定。若启动参数给出了不同的路径列表，启动失败，并提示另开新会话。

配置里的 `name` 是逻辑名称，不会变成路径的一段。命令行输入的名称是 `workspace-0`、`workspace-1`。

## 恢复

TUI：

```bash
npm run start:tui -- --v2 --resume <threadId>
```

或在界面里使用 `/resume`。

后端：

```bash
uv run --project runtime/agentkit dataagent serve --session <threadId>
```

恢复读取该会话最后一次成功的 Checkpoint。若上次运行没有完成，界面会说明已恢复到最后成功点，不会重放未完成的工作。没有成功 Checkpoint 时，需要新开对话。

同一会话同时只接受一轮运行。重复使用同一个 `runId`，或重发已经在历史里的消息，会被拒绝。

`/clear` 开始新的 `threadId`，原会话文件仍留在磁盘上。

## 查看产物

Agent 的默认工作目录是当前会话的 `outputs/`。较长的工具结果和对话卸载文件位于 `outputs/artifacts/large_tool_results` 与 `outputs/artifacts/conversation_history`。列表接口跳过这两处，也不跟随符号链接。

TUI 的 `/outputs` 读取这一目录：

- Enter 预览 UTF-8 文本，最多 64 KiB
- 二进制文件显示元数据，需要在本机打开
- 列出失败时报告错误，不会显示成空列表
- Esc 回到列表，再回到对话；`q` 关闭视图

HTTP 路径见 [HTTP 接口](../reference/rest-api.md)。

Checkpoint 可能引用这些文件。删除后，消息里指向卸载内容的 `read_file` 会找不到文件。
