# 同一 Agent 服务多个任务与产物路径设计

状态：已实现。SDK 同一 Agent 实例可执行多个会话；调用级 `output_dir`、配置 `output_root`、固定会话绑定、产物目录独占及路径校验已生效。

用户文档见 [会话与产物](../guides/sessions-and-outputs.md)。本文记录设计取舍。

## 1. 核心约定

- 同一用户、同一配置和插件版本下，复用一个 SDK Agent 实例；内部每个 session 独立创建并缓存原生执行图。
- 独立任务使用不同 `thread_id`；同一任务的后续对话沿用原 ID。
- 会话身份是 Home + user_id + thread_id。换 Home 等于换一套独立环境。
- 一个 session 对应一个固定输出目录，同一 Home 中一个输出目录也只属于一个已登记 session。该绑定在重启后仍生效，不随每轮调用或全局配置变化而切换。
- 模型客户端和语义服务客户端共享；backend、插件图、context 和路径提示词按会话绑定；tracer 绑定会话图，并按 run ID 分别保存和清理轨迹。
- 用户指定的是任务产物目录，不是 Home，也不是文件访问权限边界。

## 2. 文件归属

按文件用途分类，不按“由模型还是系统自动写入”分类。

| 内容 | 存放位置 |
| --- | --- |
| 分析脚本、清洗数据、SQL 查询结果、临时图表、最终报告 | 本次任务的 `output_dir` |
| 长工具结果、供 Agent 回读的上下文压缩历史 | `output_dir/artifacts/` |
| 会话绑定、checkpoint、日志、trace | `Home/runtime/`，沿用原有管理方式 |

长工具结果是 Agent 后续可能读取的任务材料，不视为可随意丢弃的缓存。压缩历史同理。

```text
<output_dir>/
├── analyze.py
├── report.csv
└── artifacts/
    ├── large_tool_results/
    └── conversation_history/

<home>/runtime/
├── checkpoints.sqlite
├── sessions.sqlite
└── users/<user>/sessions/<thread_id>/
    └── traces/
```

保留 `SessionPaths.artifacts = SessionPaths.outputs / "artifacts"`，无需把 artifacts 迁到 Home。SDK 显式提供的 checkpointer 仍由调用方管理存储位置。

## 3. 路径选择与 SDK 用法

新会话的优先级：

1. SDK 调用的 `output_dir`：最终目录，不追加用户或会话 ID。
2. 配置中的 `dataagent.output_root`：所有新会话产物的根目录。命名为 root，是为了和 SDK 的最终目录 `output_dir` 区分。系统在其下自动追加用户 ID 和会话 ID 两层目录，避免不同用户、不同会话的文件混在一起。例如配置为 `/data/reports`，用户为 `alice`，会话为 `task-a`，实际产物目录就是 `/data/reports/alice/task-a/`。

`output_root` 省略或为 `null` 时，使用 `<home>/outputs`，实际产物位于 `<home>/outputs/<user>/<thread_id>/`。

**配置 `output_root` 和 SDK `output_dir` 统一接受绝对路径或 `~/` 开头的路径。** `~/reports` 展开为运行进程所属操作系统用户的主目录下的 `reports`，与 DataAgent 的 `user_id` 无关。`reports`、`./reports`、`../reports` 均报错，不自动补全路径。SDK 也可用 `Path.home() / "reports"` 构造绝对路径；解析结果不依赖进程工作目录或配置文件所在目录。

**会话与输出目录绑定原则：**

| 场景 | 行为 |
| --- | --- |
| 首次创建 session | 按上述优先级确定目录，将解析后的绝对路径保存到 `session_bindings` 表 |
| 同一 session 后续调用，省略 `output_dir` | 读取已保存的目录，不重新套用全局默认值 |
| 同一 session 显式传入相同目录 | 接受；比较解析后的绝对路径，而非原始字符串 |
| 同一 session 显式传入不同目录 | 在模型和工具执行前报错，保留原绑定，不切换或搬迁文件 |
| 修改全局 `dataagent.output_root` 或重启进程 | 已有 session 保持原目录；新配置只影响此后创建的新 session |
| 新任务需要另一个输出目录 | 使用新的 `thread_id` 并指定新目录 |
| 目录已属于同一 Home 的其他已登记 session | 在执行前报错，不登记绑定 |

这是产品约定，不是 LangGraph 的限制。目的是让同一任务的后续对话始终能找到之前生成的脚本、数据和报告。

**输出目录独占：** 归属记录统一保存在 `Home/runtime/sessions.sqlite` 的 `session_bindings` 表中，由 bootstrap 管理；serve 的 `sessions` 表继续保存标题、状态和 run ID。AgentKit 不依赖 serve。

- `RuntimePaths.sessions_db` 统一文件位置，各层只创建自己的表。
- `open_session(create=True)` 只在绑定不存在时开启同步 sqlite3 短连接写事务，等待锁最多 5 秒，不切换 WAL。`BEGIN IMMEDIATE` 包住路径解析、冲突检查和插入；已有绑定只查询和校验，不重新登记或全表扫描。serve 通过 `asyncio.to_thread` 打开会话和装配 Agent，数据库等锁不阻塞事件循环。
- 同一 Home 内拒绝相同、嵌套和符号链接别名目录。macOS 保守拒绝大小写别名；不能与输入 workspace 重叠，也不能与 Home 重叠，只有 `<home>/outputs` 区域例外。
- 未登记的非空目录只允许 SDK 显式接管。不同 Home 不提供排他保证。
- 读路径不创建数据库。`session_bindings` 是绑定的唯一来源，产物目录不写控制文件。首版不提供旧文件格式的兼容读取或迁移。
- 绑定以 `(user_id, thread_id)` 唯一；REST 额外要求 thread_id 在同一 Home 全局唯一，保持现有接口约束。
- 默认产物位于 `<home>/outputs/<user>/<thread_id>/`，和 `runtime/` 分开。未来 sandbox 必须精确挂载当前会话的产物目录，不能挂整个 Home。

配置示例（合入已有配置，保留模型等字段）：

```yaml
dataagent:
  output_root: /data/reports
```

使用上面的配置，且 SDK 调用未传 `output_dir` 时：

```text
/data/reports/             # 配置中的 dataagent.output_root
└── alice/                 # user_id
    ├── task-a/            # thread_id；该任务的实际产物目录
    └── task-b/            # 另一个任务的实际产物目录
```

如果 SDK 显式传入 `output_dir="/data/reports/task-a"`，就直接使用 `/data/reports/task-a/`，不再追加目录。

SDK 用法：同一个实例执行两个任务，无须用户重载配置或创建多个 Agent。每个会话首次调用时内部构图，后续调用复用。

```python
import asyncio
from pathlib import Path

from dataagent import LaunchOptions, create_agent, prepare_runtime


async def main():
    runtime = prepare_runtime(LaunchOptions(
        config=Path.home() / ".dataagent/config.yaml",
        user_id="alice",
    ))
    async with create_agent(runtime) as agent:
        for task_id, question in (
            ("task-a", "生成三行示例销售数据，保存为 sales.csv。"),
            ("task-b", "生成三行示例库存数据，保存为 stock.csv。"),
        ):
            result = await agent.ainvoke(
                {"messages": [("user", question)]},
                config={"configurable": {"thread_id": task_id}},
                output_dir=Path.home() / "reports" / task_id,
            )
            print(result["messages"][-1].content)


asyncio.run(main())
```

**路径绑定与聊天历史分别管理：** `session_bindings` 表 保存输出目录；持久化 checkpointer 保存和恢复聊天状态。无论是否配置 checkpointer，会话的固定输出目录规则都生效。示例未配置 checkpointer，如需恢复聊天历史，另外向 `create_agent` 传入持久化 checkpointer。
外部工具通过现有 `ToolRuntime[AgentContext]` 的 `runtime.context.session.outputs` 获取本轮产物目录。

## 4. 最小实现范围

| 位置 | 调整 |
| --- | --- |
| `bootstrap/config_files.py`、`bootstrap/paths.py` | 校验绝对路径或 `~/`；保存和检查 session 的固定输出目录；在事务中登记和校验目录归属 |
| `serve/outputs.py` | 列举和预览业务产物，继续排除内部上下文卸载目录 |
| `client.py` | 按 `thread_id` 缓存原生图。统一准备会话与 context，再调用带 tracer 的会话图。同步、异步、流式与 AG-UI 共用该逻辑 |
| `models.py` | 复制会话 context，共享模型缓存、锁和语义客户端，不改共享 context 的路径 |
| `agent.py` | 继续在构图时绑定固定 backend、路径提示词和插件 `outputs_dir`；SDK 和低层接口均沿用 `build_graph` 绑定的会话 tracer |
| REST | 保留原有每会话缓存和扩展刷新。AG-UI 适配器按请求创建，操作对应会话的原生图 |

**`dataagent/core/` 不修改。** 文件、shell、Skills、Memory 和摘要仍使用 core 原生实现，不增加动态 backend 提供函数或代理。

shell 的 `cwd` 固定为该 session 的输出目录，不调用进程级 `os.chdir()`。长工具结果与压缩历史使用同一 backend 的 `artifacts/`。NL2SQL 等编译型插件在该会话构图时取得 `SubagentServices.outputs_dir`。

SDK 的 `graph` 属性取得默认会话（默认 ID 为 `sdk`）的图，首次访问会创建默认会话。读取其他会话状态用 `agent.graph_for(thread_id)`；它只读取已有会话，不创建会话，也不固定输出路径。

## 5. 验收

- 同一 SDK 实例执行两个 session，内部创建两张图；同一 session 后续调用不重复构图。模型客户端复用，文件、shell、NL2SQL、artifacts、trace 和历史分别归属对应会话。
- 恢复或修改全局配置后，已有会话仍使用原目录。省略路径、传入等价路径均可继续，显式改址在执行前报错。无 checkpointer 也遵守此规则。
- 配置和 SDK 均接受绝对路径及 `~/`，拒绝其他相对路径。校验失败不创建产物目录或会话绑定。
- 同一 Home 的两个会话不能使用相同或嵌套目录；不同 Home 不保证排他。SDK 可接管未登记的非空目录，派生目录遇到未登记文件时报错。
- 不同 session 可并发，同一 Agent 的同一 session 拒绝重叠调用；完成、异常或取消后释放。REST 扩展刷新后重建会话图，进行中的调用继续使用旧图。
- 对比本次需求开始前的基线，`dataagent/core/` 没有差异。

权限约定：Home 内的运行状态目录继续使用 `0700`。产物目录及 `artifacts/` 只创建，不修改已有目录权限；新目录和中间目录遵循进程 umask。

显式 `SessionPaths` 的 root 和 traces 必须使用当前 Home/user 的标准地址，否则在创建目录前报错。outputs 可指定其他绝对路径或 `~/`，规范化后保留。`input_workspaces=None` 沿用已有绑定，新会话使用配置默认值；显式路径列表（包括空元组）与已有绑定不同时报错，使用与 TUI 相同的路径及顺序检查。

AG-UI 适配器构造和 clone 不打开会话，收到请求后才按 `thread_id` 构图并初始化原生适配器。同步 `stream()` 提前检查会话与路径，但仅在迭代期间持续占用会话；调用方提前结束时必须显式关闭流或使用 `contextlib.closing`。

本次不增加任务管理器，不调整鉴权、sandbox 或目录权限策略。
