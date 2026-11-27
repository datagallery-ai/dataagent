# 状态与存储

[Home 目录树](home-layout.md#home-layout)列出配置、扩展及运行文件的位置。恢复会话、定位产物和分析执行过程时，需要读取不同的数据。

| 要解决的问题 | 数据在哪里 | 数据包含什么 |
| --- | --- | --- |
| 恢复一轮对话或等待中的人工反馈 | 服务端 `runtime/checkpoints.sqlite` | LangGraph 的消息、图状态和待执行位置。恢复时优先使用会话索引记录的 Checkpoint |
| 列出某个用户拥有的会话 | `runtime/sessions.sqlite` 的 `sessions` 表 | 标题、时间、最新状态、最后的运行号和 Checkpoint ID |
| 知道一个会话可以从哪里读、应把结果写到哪里 | 同一数据库的 `session_bindings` 表 | 会话创建时绑定的输入目录列表和最终产物目录 |
| 查看报告或图表 | 实际产物目录，默认 `<home>/outputs/<user>/<thread>/` | Agent 生成的业务文件，也包含被消息引用的卸载内容 |
| 分析某一轮调用的顺序、耗时与错误 | 每个会话的 `traces/` 目录 | [根执行的 OTLP JSON](execution-traces.md#local-files)，按调用关联模型、工具和子代理 |
| 分析启动、连接或接口错误 | `runtime/logs/` | 后端、网页和终端各自的[运行日志](observability.md) |

Trace 记录一轮实际执行时产生的观测数据。恢复执行使用 Checkpoint、会话绑定和依赖的业务文件。归档或迁移时，应保留原来的绑定路径以及 Checkpoint 引用的产物。只备份 Trace 或会话列表无法恢复图状态。

<a name="session-index"></a>

## 会话索引

`sessions` 主键是 `(user_id, thread_id)`，并额外要求同一 Home 内的 `thread_id` 唯一。`submitted_runs` 按 `(thread_id, run_id)` 另行登记已提交的轮次，用于拒绝重复提交。标题取用户文本压缩空白后的前 100 个字符。SDK 使用同一份绑定表，但不读写服务端会话索引。多个调用方共享 checkpointer 时，由宿主管理状态隔离。

开始一轮时写入 `running`，正常完成写入 `completed`，失败写入 `error`，取消或连接断开写入 `interrupted`。进程启动会把残留的 `running` 改为 `interrupted`。失败或取消的轮次在已有可保存的消息时更新 Checkpoint，尚未产生消息时保留原位置。

REST 暂停等待人工回答时写入 `waiting_for_input`。A2A 暂停在索引内使用 `waiting`，REST 对外显示为 `interrupted`。A2A 的默认进程内 TaskStore 重启时丢失任务记录，恢复范围见 [A2A 人工反馈](../../zh/reference/a2a.md#a2a-hitl)。REST 的读取和恢复协议见[会话详情](../../zh/reference/rest-api.md#session-detail)。

<a name="session-binding"></a>

## 绑定

`session_bindings` 以 `(user_id, thread_id)` 保存输入列表和最终产物目录。只有缺少绑定时，`open_session(create=True)` 才用短连接开启 `BEGIN IMMEDIATE`，校验产物归属并写入绑定。同一 Home 内的并发请求不能登记重叠目录。恢复已有绑定只读取本会话、校验路径并确保目录存在，无须申请写锁或扫描其他会话。`build_graph` 和 `create_agent` 都走这条路径。

绑定记录保存在 Home 的数据库中。即使产物目录位于 Home 之外，也无需在业务目录写入控制标记文件。

`for_session()` 解析已有路径而不写数据库，也不验证原始输入目录仍然可用。REST 的产物列表和预览用这条路径，因此原输入目录已被删除时仍可浏览产物。恢复执行走 `open_session()`，会重新检查绑定的输入目录是否存在且可读。

<a name="permissions-and-ownership"></a>

## 权限与归属

会话打开时，将 `runtime/` 及当前会话的状态目录设为 `0700`。产物在 Home 默认区域内时，`<home>/outputs` 及其对应的第一层目录同样设为 `0700`。其下新建的业务目录沿用进程 umask，既有业务目录和 Home 外自定义目录的权限不被重置。业务目录保留可被 Checkpoint 引用的卸载文件，使用规则见[会话与产物](../../zh/guides/sessions-and-outputs.md)。

`backend.lock` 让同一个 Home 同时只运行一个服务端。文件访问由操作系统授权。按 Home、用户和会话分配目录可以约束应用层归属，其安全含义见[安全边界](security-boundaries.md)。

实现与验证见[路径和绑定](../../../dataagent/bootstrap/paths.py)、[会话索引](../../../dataagent/server/sessions.py)、[产物目录测试](../../../tests/test_output_directory.py)与[文件访问边界测试](../../../tests/test_session_isolation.py)。
