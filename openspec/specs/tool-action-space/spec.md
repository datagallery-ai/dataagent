# 工具与 Action 空间规格

## Purpose

定义每个 Agent 的工具注册、查询、参数校验和 Workspace 访问边界，确保本地函数、MCP 与 A2A 工具通过一致的调用契约接入运行时，同时保持 Agent 实例隔离并阻止未授权的文件访问。

## Requirements

### Requirement: Per-Agent 工具隔离

系统 SHALL 为每个 Agent 维护独立的工具实例和 Schema 集合，一个 Agent 注册或发现的工具不得自动出现在其他 Agent 中。

#### Scenario: 两个 Agent 使用不同工具配置

- **WHEN** 两个 Agent 分别从不同的 `TOOLS` 配置初始化
- **THEN** 每个 Agent 只能查询和调用自身已注册或已发现的工具

### Requirement: 配置驱动的工具注册

系统 SHALL 根据 `TOOLS` 配置注册本地函数工具、MCP 工具和 A2A 工具，并为已注册工具保存可供模型使用的 Schema。

#### Scenario: 注册本地函数

- **WHEN** `TOOLS.local_functions` 声明一个可导入的 Python 函数
- **THEN** 系统将其注册为可查询、可调用且具有参数 Schema 的工具

#### Scenario: 查询未知工具

- **WHEN** 调用方查询或调用一个未注册且无法发现的工具名
- **THEN** 系统返回明确的 tool not found 错误

### Requirement: 工具参数校验

Flex Executor MUST 在执行工具前按照工具 Schema 校验参数，并阻止缺少必填参数且无法修正的调用。

#### Scenario: 缺少必填参数

- **WHEN** Flex Executor 收到的工具调用未提供 Schema 声明的必填参数
- **THEN** 参数校验失败，并返回包含缺失参数信息的错误

#### Scenario: 参数类型可安全转换

- **WHEN** Flex Executor 收到的参数类型与 Schema 不一致但属于支持的安全转换
- **THEN** 系统使用转换后的参数执行工具

#### Scenario: 包含未声明参数

- **WHEN** Flex Executor 调用的工具不接受任意关键字参数，但调用包含 Schema 未声明的参数
- **THEN** 系统移除未声明参数后执行工具

### Requirement: Workspace 配置约束

系统 MUST 拒绝相对形式的 `WORKSPACE.path` 和 `WORKSPACE.allow_path` 条目。文件类工具只能读写 Workspace；`WORKSPACE.allow_path`、Skill 根目录和已启用 Suite 根目录只能读取。

#### Scenario: 配置相对允许路径

- **WHEN** `WORKSPACE.allow_path` 包含相对路径
- **THEN** 配置加载失败，并提示允许路径必须使用绝对路径

#### Scenario: 文件工具访问未授权路径

- **WHEN** 文件类工具尝试读取 Workspace、允许路径、Skill 根目录和已启用 Suite 根目录之外的位置
- **THEN** 系统拒绝该访问，不执行对应文件操作

#### Scenario: 写入只读允许路径

- **WHEN** 文件类工具尝试写入 `WORKSPACE.allow_path`、Skill 根目录或已启用 Suite 根目录
- **THEN** 系统拒绝写入，只有 Workspace 内的目标允许写操作
