# 状态与存储

三类状态分开存放。

| 状态 | 位置 | 谁写入 |
| --- | --- | --- |
| 消息和图状态 | `<home>/runtime/checkpoints.sqlite` | LangGraph Sqlite saver |
| 会话索引 | `<home>/runtime/sessions.sqlite` | REST `Sessions` |
| 输入绑定 | `users/<user>/sessions/<thread>/session.json` | 第一次打开会话时写一次 |
| 产物 | 同会话 `outputs/` | Agent 文件工具和 shell |
| Trace | 同会话 `traces/<run_id>.json` | 根运行结束时的本地导出器 |
| 后端日志 | `<home>/runtime/logs/backend.log` | REST 日志 |
| 会话 `logs/` | 同会话目录 | 打开会话时创建该目录 |

SDK 不打开 REST 的会话索引。未指定会话时，Trace 和产物落在 `users/<user>/sessions/sdk/`。

## 会话索引

主键是 `(user_id, thread_id)`。`thread_id` 全局唯一。字段有标题、创建和更新时间、状态、最后 Checkpoint、最后 `run_id`。

`start` 把状态设为 `running`。正常结束写成 `complete` 并保存 Checkpoint id。错误写成 `error`，不推进 Checkpoint id。连接断开写成 `interrupted`。服务再次启动时，残留的 `running` 改成 `interrupted`。

标题是用户文本压缩空白后的前 100 个字符。

## 绑定

`session.json` 用 `O_EXCL` 创建，模式 `0600`。已存在的文件不会被替换。内容是 `user_id`、`thread_id` 和输入目录列表。读取时再次检查每个目录存在且可读。

## 产物

`outputs/` 和 `outputs/artifacts/` 在打开会话时创建，目录模式 `0700`。REST 列举只返回普通文件，跳过符号链接和两处内部卸载目录。

Checkpoint 可能引用这些文件。删除产物会使后续 `read_file` 找不到卸载内容。

## 归属

一个后端进程对应一个 Home 的 `runtime/`。`backend.lock` 是非阻塞排他锁。第二个进程启动失败。

`--user` 选择 `runtime/users/<user>/`。一个 `threadId` 属于创建它的 user，API 不会把它列到其他 user 下。服务监听 `127.0.0.1`。
