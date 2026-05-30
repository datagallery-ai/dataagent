## 拉取镜像

本文是数据库安装流程的第一步，用于提前准备 Docker 镜像。镜像拉取完成后，继续阅读 [数据库服务部署](service-deployment.md) 启动 MySQL、PostgreSQL 和 Elasticsearch。

若有docker pull有网络不稳定问题，多重试几次

```bash
# 使用较稳定的中转源
# 拉取 Elasticsearch 镜像
docker pull docker.m.daocloud.io/elasticsearch:7.10.1

# 拉取 PostgreSQL 镜像
docker pull docker.m.daocloud.io/library/postgres:15-alpine

# 拉取 Mysql 镜像
docker pull docker.m.daocloud.io/library/mysql:8.4

# 成功后，打回原标签以便 docker-compose 文件识别
docker tag docker.m.daocloud.io/elasticsearch:7.10.1 elasticsearch:7.10.1
docker tag docker.m.daocloud.io/library/postgres:15-alpine postgres:15-alpine
docker tag docker.m.daocloud.io/library/mysql:8.4 mysql:8.4
```
