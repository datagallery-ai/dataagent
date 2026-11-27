# 会话与产物

一次对话对应一个会话，用 `threadId`（SDK 中是 `thread_id`）标识。本文说明会话保存在哪里、产物写到哪里，以及两者之间的规则。SDK、HTTP 和 TUI 使用同一套规则。

## 两类目录

Home 的 `runtime/` 保存会话索引、输入与产物绑定、Checkpoint、日志和 Trace。默认产物位于 `<home>/outputs/<user>/<thread>/`。可以查看、复制和归档产物，运行状态由 DataAgent 管理。完整目录与权限说明见 [Home 布局](../../development/architecture/home-layout.md)和[状态与存储](../../development/architecture/state-and-storage.md#permissions-and-ownership)。

## 由 Home、用户和线程确定会话

- `user_id` 是本地用户 ID，默认是 `default`，用 `--user` 或 `LaunchOptions(user_id=...)` 选择。它不是登录账号。
- `user_id` 和 `thread_id` 长度为 1–128。首字符须为 ASCII 字母或数字，之后也可用下划线和连字符。

**换一个 Home，就是换一套独立的环境。** 新 Home 只读取自己的会话索引。下面是两个 Home 中同名会话的状态路径。

```text
~/.dataagent/runtime/users/alice/sessions/task-a/       ← Home A 里的 alice/task-a
/data/agent-home/runtime/users/alice/sessions/task-a/   ← Home B 里的 alice/task-a，是另一个会话
```

两个会话名字相同，但互不相关。要继续之前的对话，必须使用创建它时的 Home 和 user。

服务端在同一 Home 中按 `threadId` 识别会话归属。同一 ID 已属于另一用户时，请用该用户恢复或新建会话。SDK 的目录绑定以用户和线程共同区分，使用同一个 checkpointer 时由调用方安排状态隔离。

<a name="output-directory"></a>

## 产物目录的位置

新会话按下表顺序选择产物目录，使用第一个有值的来源。

| 优先级 | 来源 | 你写的值 | alice 的 task-a 实际写到 |
| --- | --- | --- | --- |
| 1 | SDK 调用参数 `output_dir` | `/srv/reports/task-a` | `/srv/reports/task-a/` |
| 2 | 配置项 `dataagent.output_root`，默认 `<home>/outputs` | `/srv/reports` | `/srv/reports/alice/task-a/` |

`output_root` 省略或设为 `null` 时，使用 `<home>/outputs`，例如 alice 的 task-a 写入 `<home>/outputs/alice/task-a/`。

- **`output_root` 是根目录。** 它对所有会话生效，所以 DataAgent 会在它下面自动追加 `<user>/<thread>/` 两层子目录，避免不同用户、不同会话的文件混在一起。
- **SDK 的 `output_dir` 是最终目录。** 它只作用于一个会话，DataAgent 不追加任何子目录。

两者都只接受绝对路径或以 `~/` 开头的路径。`~/` 指运行进程的操作系统用户的主目录，与 DataAgent 的 `user_id` 无关。`reports`、`./reports`、`../reports` 这类相对路径会报错。

## 每个会话独占一个产物目录

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

登记时会拒绝相同目录、父子目录重叠，以及指向同一目录的符号链接别名。macOS 和 Windows 上，大小写差异不能用于区分两个输出目录。

输出目录不能与输入 workspace 重叠。也不能与 Home 重叠，唯一允许的区域是 `<home>/outputs` 及其子目录。`plugins/`、`skills/`、`hooks/`、`runtime/` 和配置文件所在的根目录都不能作为产物目录。规则按解析后的实际路径判断。文件和 shell 的访问权限见[安全边界](../../development/architecture/security-boundaries.md)。

| 产物目录的状态 | SDK 显式 `output_dir` | 派生或默认目录 |
| --- | --- | --- |
| 不存在或为空，数据库没有归属记录 | 创建并登记 | 创建并登记 |
| 已登记给本会话 | 继续使用 | 继续使用 |
| 与其他已登记会话的目录相同或嵌套 | 报错 | 报错 |
| 非空、数据库没有归属记录 | 接管并保留文件 | 报错 |

空的 `artifacts/` 不算用户已有内容。已有会话的输出目录被删除后，可以按原绑定重建。

数据库登记和并发规则见[状态与存储](../../development/architecture/state-and-storage.md#session-binding)。

<a name="common-errors"></a>

## 常见报错

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `Output directory is used by session ...` | 与同一 Home 的已登记目录相同或嵌套 | 使用互不重叠的新目录 |
| `already contains files that no session owns` | 派生目录非空，但没有绑定记录 | 确认内容后，用 SDK 显式 `output_dir` 接管 |
| `overlaps Home outside its outputs area` | 输出目录覆盖 Home 的非产物区域 | 使用 `<home>/outputs` 下的目录，或 Home 外部的独立业务目录 |
| `overlaps an input workspace` | 输出与输入重叠 | 分开输入和输出目录 |
| `Session is already bound to a different output directory` | 尝试给已有会话改址 | 沿用原目录，或创建新会话 |

## 归档与迁移

迁移会话时，保留对应的 Home 运行状态和产物目录。Checkpoint 可能按原路径引用文件，因此不要通过直接修改绑定来搬迁会话。不同 Home 的产物归属记录彼此独立，由宿主协调外部产物的归档和访问。

## 输入目录

使用 YAML 的 `dataagent.input_workspaces` 为新会话设置默认输入。配置示例见[输入与工作区](inputs.md#read-only-workspaces)。

新会话把当时的输入列表写入 `session_bindings`，之后不再改写。恢复会话时继续使用这份绑定。修改 YAML 后，新会话使用新的默认目录。

自建宿主可在 `create_agent(..., input_workspaces=...)` 中指定整组输入目录。空元组 `()` 不使用输入目录。省略或传 `None` 时，新会话使用 YAML 默认值，已有会话沿用绑定。为已有会话显式传入列表时，解析后的路径和顺序必须与绑定一致。恢复时保留原名称。详见[宿主 API](../reference/python-api.md#host-api)。

配置里的 `name` 是逻辑名称，不会变成路径的一段。

## 恢复

从源码启动 TUI 时可直接恢复已保存的会话。

```bash
npm run start:tui -- --resume <threadId>
```

或在界面里使用 `/resume`。已安装客户端可按 [TUI 指南](tui.md)连接已有服务。

启动后端时也可指定已有会话。

```bash
uv run dataagent serve --session <threadId>
```

恢复留在原来的 `threadId`。优先读取索引记下的 Checkpoint，缺少时读取该线程的最新图状态。停止和连接断开都记成 `interrupted`，不另开线程。未完成的工作不会自动重放。

同一会话同时只接受一轮运行。用新的运行 ID 和消息 ID 提交下一轮。请求和人工反馈恢复规则见 [AG-UI 协议](../reference/events.md#request)。

`/clear` 开始新的 `threadId`，原会话文件仍留在磁盘上。

## 查看产物

Agent 的默认工作目录是当前会话的产物目录。较长的工具结果和对话卸载文件位于产物目录下的 `artifacts/large_tool_results` 与 `artifacts/conversation_history`。列表接口跳过这两处，也不跟随符号链接。

在 TUI 中运行 `/outputs` 查看文件。

- Enter 预览 UTF-8 文本，最多 64 KiB
- 二进制文件显示元数据，需要在本机打开
- 列出失败时报告错误，不会显示成空列表
- Esc 回到列表，再回到对话。按 `q` 关闭视图

通过 HTTP 列表或预览时见 [REST 产物接口](../reference/rest-api.md#outputs)。原输入目录失效时仍可浏览已登记的产物，但恢复执行需要能读取原输入。

Checkpoint 可能引用这些文件。删除后，消息里指向卸载内容的 `read_file` 会找不到文件。
