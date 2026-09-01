# Step2_4 最终宽表 CSV 导出

> ⛔ **这是一个独立的 todo item，不可合并到 `step2_4_validation` 或 `step2_5` 的 todo 中。**
> `step2_4_validation.sql` 未通过前禁止进入本步骤。
>
> **已知复发故障**：step2_4 校验通过后，Agent 用 `curl` 直连 ClickHouse HTTP `:8123`，认证失败后在
> `/etc/clickhouse-server/`、进程命令行、DataAgent 配置、PostgreSQL datasource、MCP 进程环境中
> 搜索凭据并暴力猜密码，耗时约 59 分钟。MCP `SELECT` + `collect_job` + 本地写 CSV 始终可用，却未被优先使用。
>
> **现网**：不涉及 ClickHouse 连接密码，无影响。本规则约束实验室/本机带鉴权的 HTTP 接口。

## 执行顺序（严格，优先级不可颠倒）

1. **默认且必须先执行**：MCP 分批查询 + 本地写 CSV（方案 A）
2. **仅当进程环境已注入非空密码时**：一次只读直连（方案 B）
3. 方案 B **首次认证失败** → 立即回到方案 A，禁止再次直连
4. 写出非空 `step2_4_wide_userfiltered.csv` 后，才能进入契约校验 / step2_5

---

## 方案 A（默认导出接口）：MCP `submit_resource_job` 分批查询 + Python 写 CSV

这是 ClickHouse MCP 资源提供的导出接口，必须先用，**不要** `curl` `:8123`。
MCP server 已持有数据库凭据，Agent 不需要密码。

1. 先查总行数（单条 `SELECT`）：

```sql
SELECT count() AS n_rows
FROM {{output_database}}.step2_4_wide_userfiltered
```

2. 按批导出，每批一条 `SELECT`，禁止 `INSERT`/`DROP`/`SHOW`/`DESCRIBE`：

```sql
SELECT *
FROM {{output_database}}.step2_4_wide_userfiltered
ORDER BY <user_id>
LIMIT 5000 OFFSET <offset>
```

`<user_id>` 用 `schema_resolution.json` 中的实际列名替换。按 `n_rows` 递增 `OFFSET`，直到取完。

3. 每次 `collect_job` 后**立即**用本地 Python（`csv` 标准库）把本批行写入
   `<当前 job workspace>/step2_4_wide_userfiltered.csv`：
   - 第一批写 header，后续批次 `append` 且不再写 header
   - 若 truncator 把完整结果落到 `tool-results/*.txt`，从该文件提取行再写 CSV，不要只根据 inline preview 落盘
4. Python **只写本地文件**。禁止 `clickhouse_connect` / `clickhouse_driver` / `requests` /
   `urllib` / 任何客户端在 Python 中连接 ClickHouse。禁止把 Python 当作 SQL 提交给 MCP。

---

## 方案 B（可选）：仅当 `CH_PASSWORD` 已预置时的一次只读直连

**前置门禁**（未通过则方案 B 不可用，直接走方案 A，不是去搜索凭据的理由）：

```bash
test -n "${CH_PASSWORD:-${CLICKHOUSE_PASSWORD:-}}"
```

- 通过：允许**一次**只读导出，SQL 必须恰好为
  `SELECT * FROM {{output_database}}.step2_4_wide_userfiltered`，
  只写入 `<当前 job workspace>/step2_4_wide_userfiltered.csv`
- 失败（`CH_PASSWORD` 未设置或为空）：立即执行方案 A

连接参数只允许读已预置的环境变量，禁止写进代码、SKILL、Suite YAML 或任何仓库文件：

| 用途 | 主变量（运行环境预置） | 兼容别名 | 未设置时的缺省 |
|------|------------------------|----------|----------------|
| host | `CH_HOST` | `CLICKHOUSE_HOST` | `127.0.0.1` |
| port | `CH_PORT` | `CLICKHOUSE_PORT` | `8123` |
| user | `CH_USER` | `CLICKHOUSE_USER` | `default` |
| password | `CH_PASSWORD` | `CLICKHOUSE_PASSWORD` | **无缺省；为空则禁用方案 B** |

本机注入方式：DataAgent 进程 `.env`（启动时加载，bash 工具继承 `os.environ`），
或 `~/.bashrc` 持久化。现网不需要配置这些变量。MCP 导出不依赖这些变量。

**首次认证失败立即切换**：HTTP 401/403、ClickHouse 代码 516、
`Authentication failed`、`password is incorrect` 等 → 立刻停止直连，改走方案 A。
禁止第二次 `curl` / `clickhouse-client` / 驱动连接。

---

## 严禁（违反即本步骤失败，必须改走方案 A）

- 扫描 `/etc/clickhouse-server/`、`users.xml`、`config.xml`
- 从进程命令行、`/proc/*/environ`、MCP server 进程环境、DataAgent YAML、
  PostgreSQL/MySQL datasource 表、workspace 配置文件中挖掘凭据
- 暴力猜密码、字典尝试、多次更换 user/password
- 为寻找凭据连续调用 bash（方案 B 最多 1 次连通性尝试）
- 直连导出源表、step2_0–step2_3 表、元数据、样本或任何其它表/文件

写出 CSV 后，表头不得含 `city` / `city_name` / `*城市*` 原列（过拟合，只保留 `{col}_tier`）。
契约检查会拦；也可先跑：

```bash
python skill/feature-engineer/scripts/step2_3_city_tier_sql.py --check-csv step2_4_wide_userfiltered.csv
```
