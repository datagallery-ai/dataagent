# Home 目录分区与会话绑定设计

状态：已实现。会话绑定存入 `sessions.sqlite`；默认产物使用 `<home>/outputs/<user>/<thread>/`，控制面会话目录只保留 `traces/`。

首版未对外发布，本设计不做新旧版本兼容：不读取旧文件格式，不迁移旧会话，不保留弃用接口。

相关文档：[同一 Agent 服务多个任务与产物路径设计](shared-agent-task-outputs.md)、[状态与存储](../architecture/state-and-storage.md)、[会话与产物](../guides/sessions-and-outputs.md)。

## 1. 问题

上一版用两个文件记录会话和产物目录的关系：

| 文件 | 位置 | 方向 |
| --- | --- | --- |
| `session.json` | Home 会话目录 | 会话 → 产物目录 |
| `.dataagent-session.json` | 产物目录 | 产物目录 → 会话 |

问题有两个：

1. **控制数据放在了 Agent 的工作目录里。** `.dataagent-session.json` 在产物目录中，Agent 的文件工具和 shell 都能读、改、删它。产物列表只是把它隐藏起来，没有阻止访问。
2. **默认产物目录在控制面内部。** 默认产物位于 `<home>/runtime/users/<u>/sessions/<t>/outputs/`。Agent 的 cwd 向上几层就是 `checkpoints.sqlite` 和 `sessions.sqlite`，`ls ..` 这类操作就能碰到控制数据。

用 Hook 禁止 Agent 访问这些文件，可以挡住一部分，但约束放错了层次。正确的做法是：控制数据只由宿主代码管理，并且从目录结构上和产物分开。

## 2. 决策

1. 会话绑定只存在 `<home>/runtime/sessions.sqlite` 的 `session_bindings` 表里。产物目录里不写任何 DataAgent 控制文件。（已实现）
2. Home 按访问方分区，默认产物移出 `runtime/`，放到 `<home>/outputs/`。（已实现）
3. `output_root` 的默认值改为 `<home>/outputs`，产物位置只剩两级规则。（已实现）
4. 删除 `SessionPaths.logs`。此前代码只创建和校验这个目录，从来不写入。（已实现）

不做的事：

- 不提供跨 Home 的产物目录排他。不同 Home 的数据库互相看不到。
- 不在非 sandbox 模式下限制 Agent 的访问，见第 9 节。
- 不新增数据库文件。绑定表和 REST 的 `sessions` 表共用 `sessions.sqlite`。

## 3. 分区

Home 下的内容按 Agent 应有的访问方式分区。以后接 sandbox 时，按这张表决定挂载方式。

| 区域 | 内容 | Agent 访问 | sandbox 挂载 |
| --- | --- | --- | --- |
| 控制面 | `runtime/` | 无 | 不挂载 |
| 宿主配置 | `config.yaml`、`.env`、`hooks/` | 无 | 不挂载 |
| 资源 | `skills/`、`plugins/` | 只读（用 `read_file` 读 SKILL.md） | 只读挂载 |
| 产物 | `outputs/` | 读写 | 只挂载本会话目录，读写 |

## 4. 目录布局

调整后：

```text
<home>/                                0700
├── config.yaml  .env                  宿主配置
├── skills/  hooks/  plugins/          扩展
├── outputs/                           0700  默认 output_root
│   └── <user>/                        0700
│       └── <thread>/                  umask  Agent cwd
│           └── artifacts/
└── runtime/                           0700  控制面
    ├── sessions.sqlite                sessions 表(REST) + session_bindings 表(bootstrap)
    ├── checkpoints.sqlite
    ├── backend.lock
    ├── logs/backend.log
    └── users/<user>/sessions/<thread>/
        └── traces/
```

调整前后：

| 项目 | 调整前 | 调整后 |
| --- | --- | --- |
| 默认产物目录 | `runtime/users/<u>/sessions/<t>/outputs/` | `outputs/<u>/<t>/` |
| 会话 `logs/` | 创建空目录 | 删除 |
| 会话目录内容 | `outputs/`、`logs/`、`traces/` | 只有 `traces/` |

`initialize_home` 不变。`outputs/` 在第一次打开会话时创建。

## 5. 产物位置

新会话按下面的顺序确定产物目录：

| 来源 | 实际位置 |
| --- | --- |
| SDK `output_dir` | 原样使用，是最终目录 |
| 配置 `dataagent.output_root`，默认 `<home>/outputs` | `<output_root>/<user>/<thread>/` |

“Home 内默认目录”这一级被删除。`output_root` 在配置里为 `null` 时，等同于 `<home>/outputs`。

实现上，`RuntimePaths.resolve` 在 `output_root is None` 时直接取 `home / "outputs"`，`RuntimePaths.output_root` 改为不可选的 `Path`。`for_session` 只剩两个分支：有绑定时用绑定，没有时用 `output_root/<user>/<thread>`。

已有会话的产物目录由绑定决定，修改 `output_root` 不影响它们。

## 6. 会话绑定（已实现）

实现在 [paths.py](../../dataagent/bootstrap/paths.py)，存储细节见 [状态与存储](../architecture/state-and-storage.md#绑定)。要点：

```sql
CREATE TABLE IF NOT EXISTS session_bindings (
    user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
    output_dir TEXT NOT NULL UNIQUE, workspaces TEXT NOT NULL,
    PRIMARY KEY (user_id, thread_id)
)
```

- 数据库位置由 `RuntimePaths.sessions_db` 决定。bootstrap 只建 `session_bindings` 表，REST 只建 `sessions` 表，两张表互相不读。
- `open_session(create=True)` 是唯一登记入口，只在绑定不存在时调用内部写入函数。写入用同步 `sqlite3` 短连接，在 `BEGIN IMMEDIATE` 事务中检查并插入，等锁最多 5 秒，不切换 WAL。
- 绑定已存在时，无论 `create` 为何值，都只查询本会话、校验路径并确保目录存在。不申请写锁、不全表扫描。SDK 续聊和 REST 重新装配都沿用这条路径；`build_graph` 和 `create_agent` 不直接写绑定表。
- 只读查询用 `mode=ro` 打开，数据库不存在时返回“无绑定”，不创建文件。
- REST 在工作线程里打开会话，数据库等锁不会阻塞事件循环。
- 并发登记同一会话时，后到的一方读取先到一方写入的绑定。

## 7. 登记规则

新会话在登记事务中按顺序检查，任何一条失败都报错，不写绑定：

1. **规范化。** `resolve()` 后比较。macOS 和 Windows 上忽略大小写，在大小写敏感的卷上可能误拒，可以接受。
2. **不和 Home 重叠，产物区除外。** 产物目录不能包含 Home，也不能位于 Home 内，唯一允许的区域是 `<home>/outputs` 及其子目录。这样同时保护 `runtime/`、`plugins/`、`skills/`、`hooks/` 和配置文件。按规范化后的实际路径判断，指向扩展目录的符号链接不能借用 `outputs/` 的例外。
3. **不和输入目录重叠。** 和每个 workspace 比较，两个方向都查。
4. **不和其他会话重叠。** 和表里所有 `output_dir` 比较，相同、包含、被包含都算。
5. **非空检查。** 推导出的目录如果已存在且非空，就拒绝；只有一个空的 `artifacts/` 不算非空。SDK 显式传入的 `output_dir` 可以接管非空目录。

`validate_outputs` 使用统一的 Home 边界规则，不逐个枚举受保护子目录。Home 之外的产物目录仍需通过输入目录和会话归属检查。

## 8. 权限

| 目录 | 权限 | 说明 |
| --- | --- | --- |
| `runtime/` 下全部目录 | 0700 | 不变 |
| `<home>/outputs` 和它下面的第一层目录 | 0700 | 新增。当产物目录位于 `<home>/outputs` 下时由 `ensure_session` 设置 |
| 会话产物目录、`artifacts/` | 遵循 umask | 不变 |
| Home 默认产物区之外的自定义 `output_root` 或 `output_dir` 及其上级 | 不修改 | 由调用方负责 |

新增这条 0700 规则的原因：原来的默认产物在 `runtime/` 下，自然是私有的。移到 `<home>/outputs` 后，如果 Home 本身不是 0700（例如 `DATAAGENT_HOME` 指向一个已有目录），在 umask 022 下默认产物会对其他 OS 用户可读。

## 9. 限制

**分区不等于隔离。** 非 sandbox 模式下，[filesystem_backend.py](../../dataagent/extensions/filesystem_backend.py) 使用 `LocalShellBackend(virtual_mode=False)`：

- 文件工具接受任意宿主机绝对路径。
- shell 以宿主进程的 OS 用户运行，`inherit_env=True`。

所以 Agent 仍然可以按绝对路径读取 `runtime/`、`.env` 等文件，换路径也无法改变这一点。分区只带来两点改善：

- Agent 在自己的 cwd 里做 `ls`、`glob`、`grep -r` 等递归操作时，不会碰到控制数据。
- 以后接 sandbox 时，产物、资源和控制面可以按第 3 节分别挂载，不需要从 `runtime/` 里挑出子目录。

真正让 Agent 看不到控制数据，需要满足两条中的一条：sandbox 不挂载控制面，或者 Agent 以另一个 OS 用户运行。

**排他检查只在同一个 Home 内有效。**

**`inherit_env=True` 会把宿主环境变量（可能包含模型 key）传给 shell。** 这个问题和本设计无关，单独跟踪。

## 10. 改动清单（已实现）

| 位置 | 改动 |
| --- | --- |
| [paths.py](../../dataagent/bootstrap/paths.py) | `RuntimePaths.output_root` 不可选，`resolve` 默认 `home / "outputs"`；`for_session` 去掉 Home 默认分支；`SessionPaths` 删除 `logs` 字段；`session_root` 只计算控制面目录，绑定查询以该目录定位数据库记录，不需要先构造产物路径；`ensure_session` 不再创建 `logs/`，并按第 8 节设置 0700；`validate_outputs` 删除默认目录豁免 |
| [client.py](../../dataagent/client.py) | 显式 `SessionPaths` 的校验只比较 root 和 traces |
| [settings.py](../../dataagent/settings.py) | 注释改为“`None` 表示 `<home>/outputs`” |
| `tests/test_output_directory.py` | 更新涉及 `logs` 和默认产物位置的断言，补充第 11 节的用例 |
| 文档 | `state-and-storage.md`、`sessions-and-outputs.md`、`configuration.md`、`reference/configuration.md`、`sdk.md`、`python-api.md`、`security-boundaries.md`、`shared-agent-task-outputs.md`：默认位置、两级规则、删除会话 `logs/` |

## 11. 验收

- 未设置 `output_root` 时，新会话产物在 `<home>/outputs/<user>/<thread>/`；`<home>/outputs` 和 `<home>/outputs/<user>` 的权限是 0700。
- 会话目录 `runtime/users/<u>/sessions/<t>/` 只包含 `traces/`。
- `output_dir` 或配置派生的产物目录与 Home 重叠时拒绝，只有 `<home>/outputs` 区域例外；符号链接指向插件或其他控制目录也会被拒绝。
- 产物目录内没有任何 DataAgent 控制文件。
- 已有绑定的会话在修改 `output_root` 后，仍使用原目录。
- 第 7 节的五条规则都有对应用例，并发登记只有一方成功。
- SDK 和 REST 首次打开会话只登记一次；续聊和重新装配不写绑定表、不全表扫描，在另一连接持有 `BEGIN IMMEDIATE` 时仍可打开已有绑定。
- 全量测试通过；`dataagent/core/` 没有改动。

验收记录：AgentKit 非 live 测试 `pytest -q -m 'not live'`：1047 通过、5 跳过、7 排除，包含底层构图路径校验、AG-UI 延迟装配、流关闭和输入目录绑定一致性用例。修改涉及的 Python 文件通过 Ruff，`git diff --check` 通过；`dataagent/core/` 无改动。
