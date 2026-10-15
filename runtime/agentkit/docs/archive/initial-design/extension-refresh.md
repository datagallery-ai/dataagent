# Session 图缓存与扩展刷新

## 用户可见行为

REST/TUI 不再为每次 query 无条件建图。同一 session 在扩展未变化时复用原生图；
扩展变化后，在下一轮请求执行前重新装配。不需要重启 TUI/后端，不清空对话。

| 修改 | 生效规则 |
| --- | --- |
| Home Skills 新增、修改、删除 | 下一轮发现并刷新 Skill 索引 |
| 已注册 Python Hook 入口文件修改 | 下一轮重新加载入口函数 |
| 在 config.json 注册/移除 Hook | 下一轮重新装配六类原生生命周期适配 |
| 安装插件并更新 plugins.enabled/paths | 下一轮读取启用插件，编译工具、Skills、子 Agent、提示词及 Hooks |
| 启用插件内的声明、Python 入口或资源修改 | 下一轮重新装配 |
| Home 或启用插件的 .mcp.json 修改 | 下一轮重新解析；连接配置变化时重新发现工具 |
| .env 或 --env-file 修改 | 下一轮重新解析扩展需要的环境引用；模型配置不热切换 |
| 模型、限额、Home、workspace、用户、服务端口修改 | 不在此热加载范围；仍需重新启动/显式准备 Runtime |

只放入 Hook 文件不等于注册，安装插件不等于启用，规则与冷启动相同。
MCP 服务端自身的工具列表变化但本地连接配置未变时，不自动重新发现；此情况目前需重启。

## 实现职责

- `bootstrap/discovery.py::extension_revision`：检查源文件/目录成员、实际路径、文件大小及纳秒级
  修改/变更时间。只检查配置、Home Skills、已注册独立 Hook 入口和启用插件资源；
  不扫描业务 workspace、session 产出，跳过 `.git`、`.venv`、`node_modules` 和缓存目录。
- `bootstrap/startup.py::refresh_extensions`：沿原来的 Home、显式 config、env-file 重新准备扩展。
  仅更新插件配置、Hooks、扩展位置和新增受保护 MCP 路径，不重新选择宿主模型、身份或 workspace。
- `restapi/agents.py::SessionAgents`：持有当前扩展版本、原生 MCP 工具及 session 图缓存。
  检查、发现 MCP、建图阶段使用同一异步锁；图执行本身不占用该锁。
- `agent.py::build_agent`：仍然只装配并返回 Deep Agents 原生图。没有嵌套 Agent runner。
- `extensions/skills.py::VersionedSkillsMiddleware`：替换原生同名 SkillsMiddleware 插槽，
  只在扩展版本变化或索引缺失时刷新派生 Skill 元数据。扫描和提示词注入仍由上游实现，
  不清空 messages，不重复安装两套 SkillsMiddleware。

图按 session ID 和已绑定的 workspace 列表缓存。单个后端固定一个 user profile，
不同 session 的 backend/output 目录不会共用。最多缓存 32 个最近使用的图；淘汰只释放
缓存引用，不删除 SQLite 历史。被淘汰的 session 再次请求时重建并继续恢复检查点。

`GET /sessions/{id}` 可以复用缓存读取历史，但不会触发热更新或连接新的 MCP。
`POST /dataagent/stream` 在本轮真正执行前检查变化。刷新成功后的插件列表反映在 `/healthz`。

## 一致性与失败

1. 使用候选 Runtime 和工具列表建图，成功后才发布新版本并使旧缓存失效。
2. 新扩展配置错误、引用缺失、Python 导入失败或 MCP 连接失败时，本次请求报 409 并给出
   `Extension reload failed`。不写入本次用户消息，不改变最后成功检查点，也不静默使用旧配置执行。
3. 修复配置后重新发送即可。加载期间源文件再次变化时，拒绝发布并提示安装完成后重试。
4. 已经执行的请求保留原有图引用，不中断、不更换已装配工具。后续新请求使用新版本。
5. session ID、检查点存储和输入绑定保持不变；进程重启后根据扩展版本刷新旧 Skill 索引。

这是“轮次边界重新装配”，不是文件内容快照。运行中直接覆盖 Skill 或工具读取的资源文件，
后续文件读取仍可能看到新内容。应先完整安装、再发送下一轮请求；本实现不提供安装事务或文件版本仓库。

## Python 和 SDK 边界

Python 入口文件每次编译直接读取源码，避免同秒同大小修改命中过期 `.pyc`。
不尝试全局 `importlib.reload`：入口依赖的普通已导入模块、第三方库或 native extension
更新仍需重启。独立 Hook 入口之外的依赖文件也不保证自动监测。

自动缓存属于 REST 宿主；SDK 返回的原生图不会自行监听文件。SDK 可复用相同公开函数，
在自己的调用边界显式刷新，并继续传原来的 checkpointer/session：

```python
from dataagent import build_agent, extension_revision, load_mcp_tools, refresh_extensions

candidate = refresh_extensions(runtime)
tools = await load_mcp_tools(candidate)
graph = build_agent(
    candidate,
    checkpointer=checkpointer,
    session=session,
    mcp_tools=tools,
    extension_revision=extension_revision(candidate),
)
runtime = candidate  # 在建图成功后替换调用方持有的配置
```

没有后台 watcher、自动重试、后台安装、动态 Python 依赖升级或用户 Hook 内的重新建图。
