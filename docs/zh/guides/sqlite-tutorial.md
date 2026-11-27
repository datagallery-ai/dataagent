# 从 SQLite 到 CSV 的第一次数据分析

用自然语言统计五笔订单在各地区的销售额。这个教程会准备样例数据库，让 DataAgent 生成 SQL 并执行，然后读取 DataAgent 写出的 CSV 来验证结果。

## 准备环境

在 DataAgent 源码仓库根目录运行。需要 Python 3.11 或 3.12、uv，以及可访问的 OpenAI 兼容模型服务。首次运行时先准备 Python 环境。

```bash
uv sync --python 3.12
```

可以复用[快速开始](../quick-start.md)中的仓库根目录 `.env`。文件需要包含 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`。如果模型变量已经在当前进程环境中设置，运行命令时可省略 `--env-file .env`。字段含义见[配置速查](../reference/configuration-api.md)。

## 执行任务

```bash
uv run --no-sync python examples/sqlite-tutorial/run.py --env-file .env
```

脚本在系统临时目录创建独立的 DataAgent Home、SQLite 文件和表结构。它通过 [Python SDK](integrate/sdk.md) 询问订单表中各地区的 `amount_cents` 总和，要求结果按总额降序排列。样例[YAML 配置](../../../examples/sqlite-tutorial/config.example.yaml)把内置 NL2SQL 设为入口，并直接向它提供这张表的结构。模型负责生成 SQL，DataAgent 在本地只读 SQLite 连接中执行 SQL。这个例子无需另行启动 Server、Web 前端或语义服务。

## 验证结果

脚本会打印 SQL、CSV 的实际路径及 CSV 内容。预期 CSV 如下，金额单位为分。

```csv
region,total_cents
north,4500
south,2300
east,800
```

NL2SQL 将查询结果写到本次 Home 的 `outputs/default/sqlite-tutorial/nl2sql/<query_id>.csv`。脚本核对表头、行序和数值后打印 `Verified: 3 region totals`。每次运行使用新的临时目录。保存所需结果后，可删除包含此次输出的 `dataagent-sqlite-*` 临时目录。

若运行时报模型连接错误，先核对模型地址和密钥。若 CSV 内容与预期不同，检查报错中的查询结果和模型配置。常见问题见[故障排查](troubleshooting.md)。可在[示例脚本](../../../examples/sqlite-tutorial/run.py)中查看样例数据和发给模型的问题。
