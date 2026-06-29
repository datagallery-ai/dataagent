# 应用案例

以下案例帮助你在不同场景下使用 DataAgent。**并非所有案例都需要 Semantic Service**——只有涉及数据库语义 / NL2SQL 的案例才需要先完成语义服务部署与场景数据导入。

## 前置条件对照

| 案例 | 是否需要 Semantic Service | 建议先阅读 |
| --- | --- | --- |
| [构建 NL2SQL 专用 Agent](build-an-nl2sql-application.md) | **是** | [快速开始 §8](../quick_start/quick_start.md#optional-semantic-service) → [Semantic Service 部署](../installation_doc/database_install/semantic-service-deployment.md) → [场景数据导入](../installation_doc/database_install/scenario-data-import.md) |
| [构建数据分析 Agent](build-a-dataagent-from-scratch.md) | **是**（当主 Agent 需要调用 NL2SQL 子 Agent 时） | 同上 |

!!! note "demo 业务库说明"
    场景教程中的 `demo_retail.sqlite` 是运行时创建的示例业务库，**不是** Semantic Layer 服务包自带内容。Agent 通过 `DATABASE.config.path` 指向其绝对路径；Semantic Service 只保存元数据。

## 案例列表

1. [构建数据分析 Agent](build-a-dataagent-from-scratch.md) — ReAct 主 Agent 按需调用 NL2SQL 子 Agent
2. [构建 NL2SQL 专用 Agent](build-an-nl2sql-application.md) — 专用 NL2SQL Agent，自然语言直接查库

## 跑通 demo 后的验证问题

完成 [场景数据导入](../installation_doc/database_install/scenario-data-import.md) 并配置 Agent 后，可尝试：

- 「各城市成交额排名」
- 「每月订单量是多少」
