# DataAgent V2 用户与多 Session 资源隔离设计

> 当前仅做资源分目录组织，不做工具访问隔离。本文的“输入只读”和“产出写入 outputs”均为 prompt 约定；文件工具与 Shell 直接使用原生 LocalShellBackend，按宿主权限执行。

## 1. 背景与目标

当前 V2 使用 LangGraph 的 `thread_id` 保存一段对话的 checkpoint，但 `thread_id`
只隔离图状态，不隔离文件系统。新的设计将用户配置的一个或多个 workspace 作为只读输入
源，将所有 Agent 写入、中间产物、日志和 trace 统一放入 Home 下的用户/session 运行目录。
这样不同 session 可以读取相同输入文件，但不会在输入 workspace 中互相覆盖或写入内部状态。

本设计引入两层业务概念：

```text
User
└── Session (thread_id)
    ├── conversation/checkpoints
    ├── read-only workspace inputs
    ├── private output root
    ├── artifacts
    └── logs
```

首版目标：

1. 每个请求属于一个明确的 `user_id`。
2. 同一用户可以拥有多个独立 session。
3. 不同 session 的输出、中间产物和日志分目录保存，Shell 默认 cwd 各自独立；不承诺文件工具或 Shell 的访问隔离。
4. `thread_id` 继续作为 LangGraph 的会话状态键，不重新实现 Agent 状态存储。
5. 一个用户可配置多个 workspace；它们按 prompt 约定作为只读输入源（不强制限制工具访问），首版不提供额外的 `shared` 目录。

首版不目标：多用户登录系统、远端身份供应商、跨 session 文件复制、可写共享目录权限
模型、分布式锁和跨主机调度。

## 2. 术语与身份边界

### 2.1 User

`user_id` 只是 Home 内部的资源隔离命名空间，不是账号、权限主体或认证结果。LangGraph
不负责这个字段，首版也不实现密码、注册、登录鉴权、SSO 或用户权限判断。

启动时通过 `--user <user-id>` 选择 Home 下的本地 profile；未指定时使用固定的
`default` profile。因此不指定 `--user` 的所有启动都落到同一个用户资源目录。profile
不存在时按初始化策略创建，不通过网络登录或密码验证。

TUI 可在启动时通过 `--workspace <path>`（可重复）为当前 profile 指定一个或多个只读输入
workspace。`--session <session-id>` 用于继续指定 session；未指定时创建新的随机 session。
这些参数只决定本地资源定位，不承担安全鉴权。

### 2.2 Session

Session 是一个业务会话，使用安全随机的 `thread_id` 作为稳定 ID。一个 session 可以
包含多轮 query；每轮请求使用新的 `run_id`，但复用同一个 `thread_id`。

```text
user_id = user_01
thread_id = 4b5...a91
run_id = 98c...2ef       # 仅这一轮执行
```

`thread_id` 不是用户 ID。相同 `thread_id` 在索引中只对应一个 `(user_id, session)` 记录，
该关系仅用于正确恢复和隔离资源，不承担跨用户的安全拒绝。

## 3. 资源布局

Home 是 DataAgent 内部状态的统一管理根。workspace 不再承载运行时状态，也不再作为
Agent 的可写根；它只提供用户业务输入。一个用户可以登记多个 workspace，系统为当前
Agent prompt 将这些真实路径标为只读输入，不创建只读挂载。

```text
<home>/
├── config.json                         # 用户级配置
├── .env                                # 用户级凭证
├── runtime/
│   ├── sessions.sqlite                  # 用户/session 索引和归属
│   ├── checkpoints.sqlite               # LangGraph checkpoint，按 thread_id 隔离
│   ├── backend.lock                     # 后端实例锁
│   └── users/
│       └── <user_id>/
│           └── sessions/
│               └── <thread_id>/
│                   ├── session.json     # session 元数据和 workspace 绑定
│                   ├── outputs/         # Agent 生成的业务/中间文件
│                   ├── logs/            # session 级日志
│                   └── traces/          # AgentKit 自动保存 <原生根 run_id>.json，标准 OTLP JSON
└── ...

<workspace-a>/                           # 用户配置的只读输入 workspace
├── orders.csv
└── source/

<workspace-b>/                           # 同一用户的另一个只读输入 workspace
└── customers.parquet
```

Home 是唯一自动配置和扩展根；workspace 仅提供只读业务输入，不从 cwd 或
workspace 下的 `.dataagent` 发现配置或扩展。显式 `--config` 可追加配置。
统一运行状态布局为：

```text
<home>/runtime/
├── sessions.sqlite
├── checkpoints.sqlite
├── backend.lock
└── users/
    └── <user_id>/
        └── sessions/
            └── <thread_id>/
                ├── session.json
                ├── outputs/
                ├── logs/
                └── traces/
```

其中：

- `sessions.sqlite` 保存用户/session 索引；
- `checkpoints.sqlite` 保存 LangGraph 状态；
- `outputs` 是当前 session 唯一可写的文件根；
- `logs` 和 `traces` 不进入任何 workspace；
- workspace 中的 `.dataagent/config.json` 不自动加载。

Agent 的文件工具和 Shell 共用真实路径，不再创建虚拟挂载：

```text
<workspace-a>（只读）
<workspace-b>（只读）
<home>/runtime/users/<user_id>/sessions/<thread_id>/outputs（可写，也是 Shell cwd）
```

主 Agent 和子 Agent 的系统提示词都提供真实输入目录和当前 session 的输出绝对路径。
文件工具使用这些绝对路径；Shell 使用相同路径或相对于 outputs 的路径，不做字符串翻译。
不得修改输入 workspace，也不得把日志、trace、checkpoint 写入输入区。

`<user_id>` 和 `<thread_id>` 都必须通过安全 opaque ID 校验，只允许字母、数字、`_`、`-`，
长度不超过 128；路径拼接后仍需检查结果位于对应父目录内。目录创建使用 0700，文件使用
0600。目录名称不使用用户显示名，避免空格、Unicode、路径穿越和重命名造成资源漂移。

多个 workspace 是用户明确配置的输入集合，不是 session 间的可写共享目录。所有 session
都可以读取这些输入；任何写入都进入自己的 session outputs。首版没有可写 `shared/`，也不支持
通过输出路径回写输入 workspace。

### 3.1 多 workspace 配置

没有通过 CLI、环境变量或配置文件指定 workspace 时，输入列表为空，不创建默认输入目录。
Home 已是统一工作根；不同用户/session 各自写入 Home 下的
`runtime/users/<user_id>/sessions/<session_id>/outputs/`。这不意味着整个 Home 对模型开放写入。

显式 workspace 原样使用，不追加 user/session 子目录。恢复已有 session 时，
沿用 `session.json` 保存的输入绑定，不迁移旧目录，也不创建新的默认 workspace。

workspace 是输入资源集合，而不是单一的可写工作目录。首版设计允许在用户配置中声明多个
已存在目录；相对路径相对于声明它的配置文件目录解析，目录必须经过宿主校验：

```json
{
  "dataagent": {
    "workspaces": [
      {
        "name": "project-data",
        "path": "/Users/me/project-data"
      },
      {
        "name": "shared-input",
        "path": "/Users/me/shared-input"
      }
    ]
  }
}
```

`name` 是展示和逻辑挂载名，不参与物理路径拼接；`path` 必须是已存在、可读取的目录。
默认情况下所有 workspace 都是只读。workspace 配置以列表为唯一产品模型；每个条目都必须
明确声明逻辑名称和物理路径。

## 4. Agent 装配方式

### 4.1 为什么不能只改 sessions.sqlite

仅给 session 表增加 `user_id` 不会改变工具的默认工作目录。
必须在 session 装配时设置原生 backend 的 cwd 和 artifacts 目录，并把输入路径传给 prompt。
这样相同的相对产出文件名落到不同 session 目录，但不阻止工具通过绝对路径访问其他目录。

### 4.2 首版推荐：按 Session 创建执行图

每次开始一轮请求时，REST 先完成归属校验并取得该 session 的目录，然后以该目录构造
本轮使用的 Agent 图：

```python
session_paths = paths.for_session(user_id, thread_id)
graph = build_agent(
    runtime,
    model=model,
    checkpointer=shared_checkpointer,
    input_workspaces=runtime.settings.workspaces,
    output_root=session_paths.outputs,
    artifacts_root=session_paths.outputs / "artifacts",
)
```

`build_agent` 的模型、插件、Hooks、限额和文件访问策略仍来自同一个 Runtime；只有执行资源
根目录按 session 变化。LangGraph 的 `checkpointer` 可以继续使用后端级
`checkpoints.sqlite`，因为状态隔离键仍是 `thread_id`。

如果后续需要降低重复建图开销，可以增加按 `(user_id, thread_id)` 缓存的已装配图；缓存
不得按用户共享一个带固定 backend root 的图，也不得把 session root 改成可变全局变量。
缓存失效、后端重启和并发请求都必须重新验证 session 归属。

### 4.3 工具和 Shell 的根目录规则

每个 session 使用独立的默认工作目录，文件操作和 Shell 直接复用原生 backend：

```python
# LocalShellBackend(root_dir=session_paths.outputs, virtual_mode=False,
#                   timeout=runtime.timeout_seconds, inherit_env=True)
# CompositeBackend(default=backend, routes={},
#                  artifacts_root=str(session_paths.artifacts))
backend = build_filesystem_backend(runtime, session_paths)
```

Shell 的 cwd、文件工具的默认 root、长工具结果 artifacts 来自同一个 `SessionPaths`，
不能分别从进程 cwd 或某个 workspace 推导。输入 workspace 按 prompt 约定只读，
产出写入当前 outputs；两者都不是访问控制规则。

不自定义 read/write/ls/glob/grep 等操作，不过滤返回结果，不收集配置/env 文件的保护路径。
文件工具与 Shell 均按宿主权限访问真实路径，可以读取私有状态、修改输入或其他 session。
原生 LocalShellBackend 负责 Shell 超时、截断和退出码。
此模式限可信本地使用；REST 下载路径校验、会话归属检查及 Home 私有目录权限仍保留。

## 5. 数据库与授权

### 5.1 sessions 表

将现有表从只按 `thread_id` 索引调整为显式归属：

```sql
CREATE TABLE sessions (
    user_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    last_checkpoint_id TEXT,
    last_run_id TEXT NOT NULL,
    PRIMARY KEY (user_id, thread_id),
    UNIQUE (thread_id)
);
```

`UNIQUE(thread_id)` 保证一个 thread 只绑定一个资源 profile。所有查询都带 session 记录中的
`user_id`，用于定位 Home 下的目录：

```sql
SELECT * FROM sessions WHERE user_id = ? AND thread_id = ?;
```

如果 `--session` 指定的 thread 不属于当前 profile，启动失败并提示选择当前 profile
已有 session 或创建新 session；这不是用户权限拒绝。

### 5.2 请求流程

```text
启动参数
 → 解析 --user（默认 default）
 → 解析一个或多个 --workspace（当前 profile 的只读输入）
 → 解析 --session（省略则创建新 session）
 → 查询 (user_id, thread_id)
 → 新 session：创建唯一 session 目录、索引和 workspace 绑定
 → 已有 session：加载其目录和已保存 workspace 绑定
 → 以 session root 装配 Agent
 → 使用 thread_id 读取/写入 checkpoint
 → 成功后更新该 profile session 的 last_checkpoint_id
```

客户端仍只能提交一条新的 user message；不能在已有 session 上替换 `user_id`/workspace。
`run_id` 只用于防 replay，不参与资源路径命名。

## 6. 并发与失败语义

1. 同一个 `(user_id, thread_id)` 同时只能有一轮执行，冲突返回 409。
2. 同一用户的不同 session 可以并发运行，因为 FilesystemBackend、Shell cwd、artifacts
   和 logs 不同。
3. 不同 profile 的 session 使用各自的 Home/profile 目录；这是资源组织规则，不是用户
   权限系统。首版不承诺多用户 Web 服务隔离。
4. 后端退出或断连时，不自动重放未完成任务；session 标记为 `interrupted`，恢复时回到
   最后成功 checkpoint。
5. 同一 session 的恢复必须重新派生并校验原来的 session root，不能接受客户端传入新的
   workspace 路径。
6. `backend.lock` 仍锁定整个 Home runtime 的后端实例；它不是 session 锁。session 锁由
   内存中的 `(user_id, thread_id)` busy 集合和数据库状态共同实现。
7. 不同 session 即使生成同名 `result.csv`，也只会出现在各自目录，不发生覆盖。

## 7. REST 与 TUI 行为

REST 接口保持现有形态；本地后端从启动参数取得 profile、session 和 workspace，不做用户
权限鉴权；本地进程间通信凭证不属于本设计的 user/session 选择流程：

- `GET /sessions`：返回当前 profile 下的 session。
- `GET /sessions/{threadId}`：按 profile/session 索引读取恢复消息。
- `POST /dataagent/stream`：使用当前 profile 的 `user_id` 和 session workspace 绑定；已有
  session 不能在请求中替换绑定。
- `/healthz`：返回实例、workspace 列表、user profile 和 session runtime 路径。

TUI 使用 `--user` 选择 profile（省略即 `default`），使用 `--workspace`（可重复）选择只读
输入，使用 `--session` 恢复指定 session（省略即新建）。`/resume` 只从当前 profile 的
列表中选择。后端按 session 保存 workspace 绑定；TUI 不负责拼接内部输出目录。

## 8. 实施范围与顺序

由于 V2 尚未对外发布，本设计直接作为首版目标实现，不保留旧目录、旧数据库 schema 或
单 workspace 可写模式。实施顺序如下：

1. 定义 `UserProfile`、`WorkspaceInput` 和 `SessionPaths`；配置层只接受 workspace 列表，
   严格校验每个名称、路径、只读属性和重复项。
2. 将 Home 初始化扩展为 runtime 根目录；创建用户/session 索引、checkpoint、输出、日志
   和 trace 的目录约束，所有内部状态只允许落在 Home runtime 下。
3. 将 Sessions API 设计为 `(user_id, thread_id)` 资源索引；启动参数解析 `--user` 和
   `--session`，不提供密码、注册、登录鉴权或权限判断。
4. 改造 Agent 装配：按 session 创建统一真实路径的 backend，直接使用原生文件操作；prompt 约定输入只读、产出写入 outputs；Shell
   cwd、artifacts 和日志全部使用同一个 `SessionPaths`。
5. 固化系统提示词和 TUI 展示：workspace 真实目录只读，session outputs 可写；恢复 session 时恢复
   相同的 workspace 列表绑定，不允许当前请求替换。
6. 增加确定性验收：多个 workspace 可读、原生操作无自定义路径拒绝、两个 session 默认输出目录独立、
   profile 切换后的资源路径正确、重启恢复和同一 session 并发返回 409。

## 9. 验收标准

- 同一用户配置 workspace A、B 后，session A、B 都能读取其中的输入文件。
- 同一用户创建 session A、B，各在自己的 outputs 写入 `result.csv`，两份文件内容保持独立。
- A 的文件工具写入后，Shell 可使用完全相同的绝对路径读取；反向同样成立。
- A 的输出可被 B 的工具通过绝对路径访问；session 分目录不是安全隔离。
- 输入 workspace 的只读要求依靠 prompt 约定；文件工具和 Shell 均不强制执行。
- Shell 可直接 `cd` 到当前 outputs 的真实绝对路径，不依赖 sandbox-exec 或 bwrap。
- 切换 profile 后不会误读另一个 profile 的 session 目录；这属于资源定位要求，不是权限拒绝。
- A 重启后使用 `--session A` 或 `/resume A` 能恢复消息和 A 的 workspace 绑定；B 不受影响。
- checkpoint、session index、工具文件和 artifacts 的关联均可通过 `(user_id, thread_id)`
  解释，不依赖当前进程内存状态。
- 现有单用户 TUI 的默认启动、`/clear`、`/resume` 和 SDK 调用行为不回归。
