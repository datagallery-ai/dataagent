## 启动数据库

本文是数据库安装流程的第二步，用于启动基础数据库服务。若镜像尚未准备好，请先阅读 [数据库镜像拉取](image-pull.md)。服务启动后，如果需要导入示例业务数据，继续阅读 [场景数据导入](scenario-data-import.md)；如果要接入 NL2SQL 的 Semantic Service metadata，继续阅读 [Semantic Service 部署指南](semantic-service-deployment.md)。

首先准备 docker compose 使用的网络和 `docker-compose-db.yaml` 文件：

```bash
docker network create datapilot-network 2>/dev/null || true
cat > ~/docker-compose-db.yaml << 'EOF'
services:
  elasticsearch:
    image: elasticsearch:7.10.1
    container_name: datapilot-es
    restart: unless-stopped
    ports:
      - "9200:9200"
      - "9300:9300"
    environment:
      - "discovery.type=single-node"
      - "xpack.security.enabled=false"
      - "ES_JAVA_OPTS=-Xms1g -Xmx1g"
      - "path.repo=/usr/share/elasticsearch/snapshots"
    ulimits:
        memlock:
          soft: -1
          hard: -1
        nofile:
          soft: 65536
          hard: 65536
    volumes:
      - ${ES_DATA_DIR:-~/es_data}:/usr/share/elasticsearch/data
    networks:
      datapilot-network:
        aliases:
          - elasticsearch
    healthcheck:
      test: ["CMD-SHELL", "curl -s http://localhost:9200/_cluster/health | grep -E '\"status\":\"(green|yellow)\"'"]
      interval: 30s
      timeout: 10s
      retries: 3
  postgres:
    image: postgres:15-alpine
    container_name: datapilot-postgres
    restart: unless-stopped
    ports:
      - "${PG_PORT:-5432}:5432"
    environment:
      POSTGRES_DB: ${DATABASE_NAME:-datapilot}
      POSTGRES_USER: ${DATABASE_USER:-datapilot_user}
      POSTGRES_PASSWORD: ${DATABASE_PASSWORD:-your_mysql_root_password}
      PGDATA: /var/lib/postgresql/data/pgdata
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./docker/postgres-init:/docker-entrypoint-initdb.d
    networks:
      datapilot-network:
        aliases:
          - postgres
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DATABASE_USER:-datapilot_user} -d ${DATABASE_NAME:-datapilot}"]
      interval: 30s
      timeout: 10s
      retries: 3
  mysql:
    image: mysql:8.4
    container_name: datapilot-mysql
    restart: unless-stopped
    ports:
      - "${MYSQL_PORT:-3306}:3306"
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD:-your_mysql_root_password}
      MYSQL_DATABASE: ${MYSQL_DATABASE:-appdb}
      MYSQL_USER: ${MYSQL_USER:-app}
      MYSQL_PASSWORD: ${MYSQL_PASSWORD:-app_pass}
      MYSQL_ALLOW_EMPTY_PASSWORD: "no"
      TZ: Asia/Shanghai
    command:
        - --character-set-server=utf8mb4
        - --collation-server=utf8mb4_0900_ai_ci
        - --default-time-zone=+8:00
        - --sql-mode=STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION
        - --max-connections=1000
    volumes:
      - mysql_data:/var/lib/mysql
      - ./docker/mysql-init:/docker-entrypoint-initdb.d/
      - ./docker/mysql-conf:/etc/mysql/conf.d/
    networks:
      datapilot-network:
        aliases:
          - mysql
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost", "-u", "root", "-p${MYSQL_ROOT_PASSWORD:-your_mysql_root_password}"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
volumes:
  es_data:
  postgres_data:
  mysql_data:
networks:
  datapilot-network:
    external: true
    name: datapilot-network
EOF
```

然后设置vm.max_map_count

```bash
sudo chmod -R 777 ~/es_data
[ $(cat /proc/sys/vm/max_map_count) -lt 262144 ] && (echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf && sudo sysctl -p) || echo "vm.max_map_count 已经符合要求，无需修改。"
```

然后启动&验证数据库

```bash
# 启动数据库
cd ~
docker compose -f ~/docker-compose-db.yaml up -d

# 验证数据库是否启动成功
# 查看容器状态
docker ps

# 连接数据库（连接后退出使用Ctrl+D）
sudo docker exec -it datapilot-mysql mysql -uapp -papp_pass
sudo docker exec -it datapilot-postgres psql -U datapilot_user -d datapilot
curl localhost:9200
```

若需要停止并删除数据库&卷&网络，执行↓

```bash
cd ~
docker compose -f ~/docker-compose-db.yaml down -v
```
