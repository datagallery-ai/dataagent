# 数据库安装指导

本节文档按不同目标组织。**Semantic Service（语义层 REST 服务）是 DataAgent 的外部可选组件**，用于 NL2SQL 与数据库语义增强；启动 Agent 本身不依赖本节内容。

## 推荐阅读路径

| 目标 | 阅读顺序 |
| --- | --- |
| 只想跑通 Agent | [快速开始](../../quick_start/quick_start.md) 主链路即可，无需阅读本节 |
| 需要 NL2SQL / 数据库语义能力 | [Semantic Service 部署指南](semantic-service-deployment.md) → [场景数据导入](scenario-data-import.md) → [应用案例](../../case/case.md) |
| 需要 MySQL / PostgreSQL / Elasticsearch 基础环境 | [数据库镜像拉取](image-pull.md) → [数据库服务部署](service-deployment.md)（扩展场景，非 Semantic Service 必需） |

## 文档索引

### 语义服务（NL2SQL 可选组件）

1. [Semantic Service 部署指南](semantic-service-deployment.md) — 独立服务包、PostgreSQL/pgvector、向量模型、启动与验证
2. [场景数据导入](scenario-data-import.md) — demo 业务库、元数据 bulk 导入、检索 API 验证

### 基础数据库环境（扩展）

1. [数据库镜像拉取](image-pull.md)
2. [数据库服务部署](service-deployment.md)
