# Agent 配置与启动规格

## Purpose

定义 DataAgent 从 YAML 配置生成隔离 Agent 实例时的配置合并、默认值和运行引擎选择行为。

## Requirements

### Requirement: 分层配置合并

系统 SHALL 按默认配置、已启用 Suite 配置和用户配置从低到高的优先级生成每个 Agent 的最终配置，且用户配置具有最高优先级。

#### Scenario: 用户值覆盖低优先级标量

- **WHEN** 用户配置与默认配置或 Suite 配置定义同一个标量路径
- **THEN** 最终配置使用用户配置中的值

#### Scenario: 工作流节点按名称合并

- **WHEN** 多个配置层在 `ACTOR_LOOP`、`PRE_WORKFLOW` 或 `POST_WORKFLOW` 中定义同名节点
- **THEN** 系统按节点名称合并配置，并由高优先级层覆盖同名字段

### Requirement: 从 YAML 创建独立 Agent

`DataAgent.from_config` SHALL 从指定 YAML 加载配置，并为新 Agent 创建独立的 `ConfigManager`，不得把该 Agent 的配置写入模块级共享配置。

#### Scenario: 创建两个配置不同的 Agent

- **WHEN** 调用方分别使用两个 YAML 文件创建两个 DataAgent
- **THEN** 每个 Agent 持有自己的最终配置，且一个实例的配置变更不影响另一个实例

### Requirement: 默认 Agent 类型

系统 SHALL 在最终配置未指定 `AGENT_CONFIG.type` 时使用 `react`。

#### Scenario: 未配置 Agent 类型

- **WHEN** YAML 和低优先级配置均未提供 `AGENT_CONFIG.type`
- **THEN** DataAgent 以 `react` 类型完成初始化

### Requirement: 按类型选择运行引擎

系统 MUST 根据 `AGENT_CONFIG.type` 选择运行引擎：`react` 使用 FlexAgent，`nl2sql` 使用 NL2SQLAgent；其他值必须被拒绝。

#### Scenario: 创建 ReAct Agent

- **WHEN** 最终配置中的 `AGENT_CONFIG.type` 为 `react`
- **THEN** DataAgent 在首次使用对话引擎时创建 FlexAgent

#### Scenario: 不支持的 Agent 类型

- **WHEN** 最终配置中的 `AGENT_CONFIG.type` 不是受支持的类型
- **THEN** 系统返回明确的 unsupported agent type 错误
