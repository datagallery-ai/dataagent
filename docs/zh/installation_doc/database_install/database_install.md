# 数据库安装指导

本节文档按“先准备基础数据库，再导入业务数据，最后接入 Semantic Service”的顺序组织：

1. [数据库镜像拉取](image-pull.md)
2. [数据库服务部署](service-deployment.md)
3. [场景数据导入](scenario-data-import.md)
4. [Semantic Service 部署指南](semantic-service-deployment.md)

推荐阅读路径：

| 目标 | 阅读顺序 |
| --- | --- |
| 只想启动 MySQL / PostgreSQL / Elasticsearch | 先看 1、2。 |
| 想导入零售示例数据 | 看完 1、2 后继续看 3。 |
| 想让 NL2SQL 使用表字段语义和 join 关系 | 看完 1、2 后继续看 4；如果要用示例业务数据，再加上 3。 |
