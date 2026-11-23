# 状态与存储

三类状态分开存放。

| 状态 | 位置 | 谁写入 |
| --- | --- | --- |
| 消息和图状态 | `<home>/runtime/checkpoints.sqlite` | LangGraph Sqlite saver |
| 会话索引 | `<home>/runtime/sessions.sqlite` | REST/A2A 共用的 `Sessions` |
| 输入和输出绑定 | `sessions.sqlite` 的 `session_bindings` 表 | `dataagent.bootstrap.paths` |
| 产物 | 默认 `<home>/outputs/<user>/<thread>/`，可配置 | Agent 文件工具和 shell |
| Trace | `<home>/runtime/users/<user>/sessions/<thread>/traces/<run_id>.json` | 根运行结束时的本地导出器 |
| 后端日志 | `<home>/runtime/logs/backend.log` | 服务入口、插件和启动器 |
| 界面日志 | `<home>/runtime/logs/web.log`、`tui.log` | 对应 Web/TUI 进程或其启动器 |

SDK 与服务端共用 `session_bindings` 的产物绑定规则，但不读写服务端的 `sessions` 索引表；SDK 的对话 Checkpoint 由调用方提供的 checkpointer 管理，不自动使用服务端的 `checkpoints.sqlite`。SDK 未指定会话时使用 `sdk` 作为 thread ID，按上表分别保存 Trace 和产物。控制面的会话目录只保留 `traces/`，不创建 `logs/` 或 `outputs/`。

## 会话索引

主键是 `(user_id, thread_id)`。`thread_id` 全局唯一。字段有标题、创建和更新时间、状态、最后 Checkpoint、最后 `run_id`。

`start` 把状态设为 `running`。正常结束写成 `completed` 并保存 Checkpoint id。错误写成 `error`，取消或连接断开写成 `interrupted`；收尾时若本轮已有消息 Checkpoint，也保存它的位置，没有新消息时保留原位置。服务再次启动时，残留的 `running` 改成 `interrupted`。

A2A TaskStore 是进程内的协议任务记录。重启后旧 Task ID 不可查询，同一 `contextId` 对应的会话、Checkpoint 和产物仍可继续使用；它不构成另一套持久业务记录。

标题是用户文本压缩空白后的前 100 个字符。

## 绑定

`session_bindings` 是 user、thread、输入列表和输出目录绑定的唯一来源。只有 `open_session(create=True)` 在缺少绑定时登记：短连接在 `BEGIN IMMEDIATE` 事务中检查目录重叠并插入，不切换日志模式。绑定已存在时只查询本会话、校验路径并确保目录存在，不申请写锁或扫描其他会话。`build_graph` 和 `create_agent` 都通过 `open_session` 准备会话，不直接写表。

只读查询不创建数据库；首版不读取或迁移旧文件格式。恢复执行时检查输入目录存在且可读。REST 将打开会话和装配 Agent 都放到工作线程，避免同步 I/O 阻塞事件循环。

## 产物

产物目录和其中的 `artifacts/` 在打开会话时创建。未指定 SDK `output_dir` 时使用 `<output_root>/<user>/<thread>/`，`output_root` 默认是 `<home>/outputs`。

Home 的 `outputs/` 及其第一层目录固定为 `0700`。会话产物目录和 `artifacts/` 保留已有权限，新建时遵循进程 umask；Home 默认产物区之外的自定义目录及上级权限不修改。产物不能与 Home 重叠，只有 `<home>/outputs` 区域例外。REST 列举只返回普通文件，跳过符号链接和两处内部卸载目录。

Checkpoint 可能引用这些文件。删除产物会使后续 `read_file` 找不到卸载内容。

## 归属

一个后端进程对应一个 Home 的 `runtime/`。`backend.lock` 是非阻塞排他锁。第二个进程启动失败。

`--user` 选择 `runtime/users/<user>/`。一个 `threadId` 属于创建它的 user，API 不会把它列到其他 user 下。服务监听 `127.0.0.1`。
