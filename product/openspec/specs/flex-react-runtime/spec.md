# Flex/ReAct 运行时规格

## Purpose

定义 Flex Agent 从配置构建工作流、执行 Actor 循环以及结束一次运行的核心行为，包括 Actor 节点构建、循环路由、完成路由、人工反馈和运行限制。该规格用于记录当前实现能够验证的运行语义。

## Requirements

### Requirement: 配置驱动的 Actor 工作流

系统 SHALL 根据 `ACTOR_LOOP` 构建 Actor 工作流，并要求至少包含一个可实例化的 Actor 节点。

#### Scenario: 按配置创建 Actor 节点

- **WHEN** `ACTOR_LOOP` 中的节点包含有效名称和可导入模块路径
- **THEN** 系统按配置创建节点并将其加入 Actor 工作流

#### Scenario: Actor 工作流为空

- **WHEN** 配置未提供任何 Actor 节点
- **THEN** 系统拒绝创建 Flex Agent，并返回缺少 Actor 节点的错误

### Requirement: Actor 循环

系统 SHALL 在状态未完成且未达到限制时，从 Actor 工作流的最后一个节点路由回第一个节点继续执行。

#### Scenario: 继续规划与执行

- **WHEN** 最后一个 Actor 节点完成且状态中的 `complete` 为 false
- **THEN** 路由器返回第一个 Actor 节点，开始下一轮循环

### Requirement: 正常完成

系统 SHALL 在状态中的 `complete` 为 true 时退出 Actor 循环，并进入首个后置节点；没有后置节点时直接结束。

#### Scenario: 有后置工作流

- **WHEN** Actor 节点执行后将 `complete` 设置为 true，且配置了后置节点
- **THEN** 路由器进入首个后置节点

#### Scenario: 无后置工作流

- **WHEN** Actor 节点执行后将 `complete` 设置为 true，且未配置后置节点
- **THEN** 路由器结束工作流

### Requirement: 运行限制

系统 MUST 在配置了最大迭代次数或 token 上限且运行达到对应限制时停止 Actor 循环，并保留当时的运行状态供上层生成结果。

#### Scenario: 达到最大迭代次数

- **WHEN** `curr_iter` 达到显式配置的 `max_iter` 且状态尚未完成
- **THEN** 系统抛出包含当前状态的限制错误，不再进入下一轮 Actor 循环

#### Scenario: 达到 token 上限

- **WHEN** 消息累计 token 数达到显式配置的 `token_limit` 且状态尚未完成
- **THEN** 系统抛出包含当前状态的限制错误，不再进入下一轮 Actor 循环

### Requirement: 可选人工反馈

系统 SHALL 在配置至少两个 Actor 节点、启用人工反馈且当前状态请求反馈时，将首个 Actor 节点之后的执行路由到人工反馈节点；反馈完成后重新进入首个 Actor 节点。

#### Scenario: 请求人工反馈

- **WHEN** 配置至少两个 Actor 节点，`enable_human_feedback` 和 `need_human_feedback` 均为 true，且当前轮次尚未进入人工反馈
- **THEN** 系统进入人工反馈节点，并在反馈完成后重新规划
