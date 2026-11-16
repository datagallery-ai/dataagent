# 会话与产物

一次对话对应一个会话，用 `threadId`（SDK 中是 `thread_id`）标识。本文说明会话保存在哪里、产物写到哪里，以及两者之间的规则。SDK、HTTP 和 TUI 使用同一套规则。

## 两类目录

DataAgent 把文件分成两类，分别放在两个位置：

| 类别 | 位置 | 内容 | 谁来管理 |
| --- | --- | --- | --- |
| 运行状态 | Home 的 `runtime/` 目录 | 会话绑定 `session_bindings` 表、会话索引、Checkpoint、日志、Trace | DataAgent。不要手动修改 |
| 产物 | 每个会话的产物目录 | 分析脚本、数据、图表、报告，以及 `artifacts/` 下的长工具结果和对话卸载文件 | 你。可以查看、复制、归档 |

运行状态目录的权限固定为 `0700`，只有当前操作系统用户可以访问。Home 的 `outputs/` 和其第一层目录也固定为 `0700`。会话产物目录及 `artifacts/` 保留已有权限，新建时使用进程 umask；Home 默认产物区之外的自定义目录及其上级权限不修改。

Home 默认是 `~/.dataagent`，可以用环境变量 `DATAAGENT_HOME` 改变。

## 会话身份：Home + user + thread

一个会话由三部分共同确定：

```text
会话 = Home + user_id + thread_id
```

- `user_id` 是本地用户 ID，默认是 `default`，用 `--user` 或 `LaunchOptions(user_id=...)` 选择。它不是登录账号。
- `user_id` 和 `thread_id` 只能包含字母、数字、下划线和连字符，最长 128 个字符。

**换一个 Home，就是换一套独立的环境。** 新 Home 看不到旧 Home 的会话，这和 git 仓库或浏览器 profile 的做法相同。例如：

```text
~/.dataagent/runtime/users/alice/sessions/task-a/       ← Home A 里的 alice/task-a
/data/agent-home/runtime/users/alice/sessions/task-a/   ← Home B 里的 alice/task-a，是另一个会话
```

两个会话名字相同，但互不相关。要继续之前的对话，必须使用创建它时的 Home 和 user。

## 运行状态目录

```text
<home>/runtime/
  checkpoints.sqlite
  sessions.sqlite       ← session_bindings（绑定）与 sessions（REST 索引）
  backend.lock
  logs/backend.log
  users/<user>/sessions/<thread>/
    traces/<run_id>.json
```

默认产物独立存放在 `<home>/outputs/<user>/<thread>/`，其中的 `artifacts/` 保存长工具结果和压缩历史。初始化 Home 时不创建产物目录；第一次打开会话时才创建。

`RuntimePaths.sessions_db` 提供数据库地址。SDK 和 REST 共用这个文件：AgentKit 管理 `session_bindings` 表，serve 管理 `sessions` 表，彼此不读写对方的表。

绑定主键是 `(user_id, thread_id)`。REST 索引额外要求同一 Home 内 `thread_id` 全局唯一，这是现有 HTTP/Checkpoint 协议的约束，本次保持不变。SDK 的绑定允许不同 user 使用相同 thread；共享 checkpointer 时仍需由宿主安排状态隔离。

## 产物目录的位置

新会话按下面的顺序确定产物目录，使用第一个有值的来源：

| 优先级 | 来源 | 你写的值 | alice 的 task-a 实际写到 |
| --- | --- | --- | --- |
| 1 | SDK 调用参数 `output_dir` | `/srv/reports/task-a` | `/srv/reports/task-a/` |
| 2 | 配置项 `dataagent.output_root`，默认 `<home>/outputs` | `/srv/reports` | `/srv/reports/alice/task-a/` |

`output_root` 省略或设为 `null` 时，使用 `<home>/outputs`，例如 alice 的 task-a 写入 `<home>/outputs/alice/task-a/`。

注意两者的区别：

- **`output_root` 是根目录。** 它对所有会话生效，所以 DataAgent 会在它下面自动追加 `<user>/<thread>/` 两层子目录，避免不同用户、不同会话的文件混在一起。
- **SDK 的 `output_dir` 是最终目录。** 它只作用于一个会话，DataAgent 不追加任何子目录。

两者都只接受绝对路径或以 `~/` 开头的路径。`~/` 指运行进程的操作系统用户的主目录，与 DataAgent 的 `user_id` 无关。`reports`、`./reports`、`../reports` 这类相对路径会报错。

## 规则：每个会话独占一个产物目录

> 每个会话独占一个产物目录。目录在会话创建时确定，之后不变。

| 场景 | 结果 |
| --- | --- |
| 创建会话 | 按上面的优先级确定目录，写入 `session_bindings` |
| 恢复会话，不传 `output_dir` | 使用数据库里保存的目录 |
| 恢复会话，传入相同的目录 | 接受。比较的是解析后的绝对路径 |
| 恢复会话，传入不同的目录 | 在执行前报错。原绑定不变，已有文件不搬迁 |
| 修改配置 `output_root` 或重启 | 已有会话保持原目录。新配置只影响之后创建的会话 |
| 产物目录被删除后恢复会话 | 重新创建空目录，会话继续使用它 |
| 需要换一个目录 | 使用新的 `thread_id` |

“独占”只在同一个 Home 已登记的会话之间检查，不提供跨 Home 排他保证。

## 目录归属

登记在一个数据库事务中完成。除了完全相同的目录，也拒绝父子目录重叠和符号链接别名。macOS 上保守地把大小写不同的路径视为同一路径，因此在区分大小写的 macOS 卷上也不能用大小写来区分两个输出目录。

输出目录不能与输入 workspace 重叠。也不能与 Home 重叠，唯一允许的区域是 `<home>/outputs` 及其子目录；`plugins/`、`skills/`、`hooks/`、`runtime/` 和配置文件所在的根目录都不能作为产物目录。规则按解析后的实际路径判断，不能用符号链接绕过。未来接入 sandbox 时，应精确挂载业务输出目录，不能挂载整个 Home。

| 产物目录的状态 | SDK 显式 `output_dir` | 派生或默认目录 |
| --- | --- | --- |
| 不存在或为空，数据库没有归属记录 | 创建并登记 | 创建并登记 |
| 已登记给本会话 | 继续使用 | 继续使用 |
| 与其他已登记会话的目录相同或嵌套 | 报错 | 报错 |
| 非空、数据库没有归属记录 | 接管并保留文件 | 报错 |

空的 `artifacts/` 不算用户已有内容。已有会话的输出目录被删除后，可以按原绑定重建。

`session_bindings` 是绑定的唯一来源，产物目录中不写入控制文件。`open_session(create=True)` 是唯一登记入口，只在绑定不存在时开启写事务。已有绑定的打开、续聊和重新装配只查询本会话、校验路径并确保目录存在，不重新登记或扫描其他会话。`for_session` 和 `open_session(create=False)` 不创建数据库或绑定记录。首版直接使用这个存储结构，不提供旧文件格式的兼容读取或迁移。

## 常见报错

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `Output directory is used by session ...` | 与同一 Home 的已登记目录相同或嵌套 | 使用互不重叠的新目录 |
| `already contains files that no session owns` | 派生目录非空，但没有绑定记录 | 确认内容后，用 SDK 显式 `output_dir` 接管 |
| `overlaps Home outside its outputs area` | 输出目录覆盖 Home 的非产物区域 | 使用 `<home>/outputs` 下的目录，或 Home 外部的独立业务目录 |
| `overlaps an input workspace` | 输出与输入重叠 | 分开输入和输出目录 |
| `Session is already bound to a different output directory` | 尝试给已有会话改址 | 沿用原目录，或创建新会话 |

## 不推荐的用法

- **让多个任务共用一个产物目录。** 同名文件和 `artifacts/` 会互相覆盖，DataAgent 会拒绝第二个会话。需要放在一起时，使用同一父目录下的不同子目录，例如 `~/reports/task-a`、`~/reports/task-b`。
- **用相同的 `thread_id` 表示不同的任务。** `thread_id` 是会话的名字，同一个名字会恢复同一个会话，并沿用它的产物目录。新任务请使用新的 `thread_id`。
- **期望换 Home 后还能找到原来的会话。** Home 是独立的环境。迁移会话需要同时保留 Home 的 `runtime/` 和产物目录。
- **手动修改数据库绑定来“搬家”。** 这会让 Checkpoint 中引用的文件路径失效。需要新位置时，开一个新会话。

## 已知限制

- 不同 Home 不共享归属记录。跨 Home 使用同一目录需由宿主协调，尤其不要同时写入。
- 数据库绑定是应用级约束，不是文件系统沙箱。外部程序仍可移动目录或修改链接；删除 Home 也会删除绑定，但不会删除外部产物。

## 输入目录

输入来源由高到低：

1. 重复的 `--workspace`
2. 环境变量 `DATAAGENT_WORKSPACE`
3. 配置 `dataagent.workspaces`

都没有时，列表为空。显式路径必须已经存在，并且进程可以读取和进入。

新会话把当时的输入列表写入 `session_bindings`，之后不再改写。恢复会话时继续使用这份绑定。若启动参数给出了不同的路径列表，启动失败，并提示另开新会话。

SDK 的 `input_workspaces` 使用同一规则：省略或传 `None` 时沿用绑定；显式列表（包括空元组 `()`）必须匹配解析后的路径和顺序，不同则报错，不静默忽略。名称不参与比较，恢复时仍保留原名称。

配置里的 `name` 是逻辑名称，不会变成路径的一段。命令行输入的名称是 `workspace-0`、`workspace-1`。

## 恢复

TUI：

```bash
npm run start:tui -- --resume <threadId>
```

或在界面里使用 `/resume`。

后端：

```bash
uv run --project runtime/agentkit dataagent serve --session <threadId>
```

恢复留在原来的 `threadId`。行上有 Checkpoint 时读取它；没有时读取该线程最新图状态。停止和连接断开都记成 `interrupted`，不另开线程。未完成的工作不会自动重放。

同一会话同时只接受一轮运行。重复使用同一个 `runId`，或重发已经在历史里的消息，会被拒绝。

`/clear` 开始新的 `threadId`，原会话文件仍留在磁盘上。

## 查看产物

Agent 的默认工作目录是当前会话的产物目录。较长的工具结果和对话卸载文件位于产物目录下的 `artifacts/large_tool_results` 与 `artifacts/conversation_history`。列表接口跳过这两处，也不跟随符号链接。

TUI 的 `/outputs` 读取这一目录：

- Enter 预览 UTF-8 文本，最多 64 KiB
- 二进制文件显示元数据，需要在本机打开
- 列出失败时报告错误，不会显示成空列表
- Esc 回到列表，再回到对话；`q` 关闭视图

HTTP 路径见 [HTTP 接口](../reference/rest-api.md)。

Checkpoint 可能引用这些文件。删除后，消息里指向卸载内容的 `read_file` 会找不到文件。
