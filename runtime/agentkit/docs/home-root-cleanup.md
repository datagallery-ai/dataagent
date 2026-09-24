# Home 统一工作根：设计与执行

## 目标与边界

移除 project 作用域，不再从启动目录或业务 workspace 自动加载配置、Skills、Hooks、Plugins。
不增加新的路径参数、配置层或兼容分支。本轮不实现 MCP 连接、工具发现或认证。

## 唯一规则

- Home 是统一工作根：使用进程 `DATAAGENT_HOME`，未设置时为 `~/.dataagent`。
  切换 Home 后，默认配置、扩展、状态、session 输出路径一起切换；不搬迁原有数据。
- `workspaces` 仅表示额外的只读业务输入，可以为空。不再自动创建 `home/workspaces/<user>`。
- Agent 的可写范围仍为 `<home>/runtime/users/<user>/sessions/<session>/outputs`。
  Home 统一管理不等于整个 Home 对模型开放读写；保留文件权限与 session 隔离。
- `cwd` 只用于解析显式输入的相对路径，不发现项目根、不读取 `cwd/.dataagent`。
- 配置优先级为内置默认值 < `<home>/config.json` < 显式 `--config`。
  保留现有字段合并和环境变量规则；`--config` 不改变 Home，也不发现相邻扩展目录。
- Skills、Plugins 的自动来源仅为现有内置资源及 Home；保留显式插件路径和配置中的 Hook 引用。
- 新 session 没有额外输入时保存空列表；恢复 session 继续使用已保存的输入绑定，不自动改写旧数据。

## MCP 来源约定（后续实现）

后续状态：MCP 接入已按这两处来源实现，具体格式、生命周期和验收见 [mcp.md](mcp.md)。
下述执行记录保留路径清理阶段的边界。

仅支持 `<home>/.mcp.json` 与启用插件的 `<plugin_root>/.mcp.json`。
不读取 cwd、业务 workspace、session 目录中的 `.mcp.json`，不因为 `--config` 改变发现位置。
本轮只固定来源边界，不创建空文件、不增加未生效的 MCP 配置项。

## 执行步骤

1. **bootstrap**：删除 `project_config_dir`、`project_mode`、项目发现/信任类型及报告字段；
   配置层收口 Home + 显式文件，扩展来源移除 project；允许空输入列表，移除默认输入目录。
2. **REST / CLI**：删除项目相关参数和信任握手；保留就绪消息、实例身份及父进程退出清理。
   `/healthz` 返回 `home` 和只读 `workspaces`，不再用第一个输入冒充默认工作根。
3. **TUI**：删除项目选择提示和状态展示，默认直接等待后端就绪；工作根显示 Home，
   输入挂载单独展示。保留相对路径解析、超时、错误脱敏和子进程退出处理，不改聊天布局。
4. **文档与测试**：更新 SDK、CLI、配置样例和架构说明；用“cwd/输入目录不注入能力”
   的回归替换旧项目信任测试，保留 Home、显式配置、插件及 session 的测试。

## 验收

- 相同 Home 与绝对显式参数，在不同 cwd 启动得到相同配置及扩展；损坏的
  `cwd/.dataagent` 或 workspace 配置不影响启动，其 Python 扩展不执行。
- Home 切换同时切换配置、扩展与状态根；相对 CLI 路径仍按启动目录解析。
- 不提供 workspace 也能启动、写出文件、保存并恢复 session；额外输入仍只读。
- TUI 启动不等待信任选择；就绪超时、错误、退出和流式会话回归通过。
- 不删除或迁移用户现有 Home、workspace、session 文件；不提交或推送。

## 执行状态

已完成上述清理。删除项目发现/信任实现与握手，未新增配置层或执行抽象；配置只读取一遍。
保留现有文件 backend、Shell、session 输出权限与相对路径解析规则。

- 后端全量非 live 测试：381 passed、1 deselected；未调用真实模型服务。
- TUI 构建与测试：194 passed；后端用例另覆盖 60/80 列真实 PTY 启动、退出与终端恢复。
- 新增验证：不同 cwd 配置/扩展一致；Home 切换；空输入写出文件、SQLite 恢复后再次读取；
  保留显式输入与旧 session 绑定。
- 分层测试补齐已有 filesystem host adapter 的依赖，并修正大小写不敏感文件系统下的类/模块识别；
  单独复测 35 passed。
- Ruff 通过（沿用排除既有格式问题的 `tests/test_tui_cursor.py`）；`git diff --check` 通过。
- MCP 仍仅确定两处配置来源，尚未接入连接/工具加载；未改写用户 Home 数据，未提交或推送。
