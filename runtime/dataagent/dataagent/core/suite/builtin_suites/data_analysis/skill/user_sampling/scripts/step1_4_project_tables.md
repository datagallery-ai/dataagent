# step1_4: project_tables（全表投影）

<入口规则>两种模式均执行本步</入口规则>

**目的**：为 `projections[]` 中每张源表在 `output_database` 建<必须>同名</必须>交付表。列集与源表一致；用户表含 `label` 列（主路径 JOIN 追加 / prelabeled 保留源列）。

<必须>ClickHouse SQL 仅通过 `submit_resource_job`（`resource_id="clickhouse"`）执行。</必须>

<必须>表数 = `projections[]` 项数 = `inventory_check.table_count`，少一张不得进入 step1_5</必须>。`step1_temp_*` 不参与计数。

## 前置

step1_3 已完成，`step1_temp_sampled_users` 可查。`step1_0_table_schema.json` 与 `step1_0_sampling_plan.json` 已落盘。ENGINE 固定 `MergeTree() ORDER BY tuple()`。

投影 SQL **由脚本生成**，不要手写 CTAS。

```bash
python skill/user_sampling/scripts/build_projection_sql.py \
  --plan step1_0_sampling_plan.json \
  --schema step1_0_table_schema.json \
  --out-dir step1_4_sql
```

退出码 0 后，工作区有：

- `step1_4_sql/step1_4_ctas_manifest.json`（`table_count` / 每张表的 `file` 与 `type`）
- `step1_4_sql/<index>_<table>.sql`（每张交付表一条 `CREATE OR REPLACE TABLE ... AS SELECT`）
- `step1_4_sql/step1_4_gate.sql`
- `step1_4_sql/step1_4_count_check.sql`

退出码 2：按 stderr 修 plan（缺 `user_key`、`game_keyed` 误标、缺
`game_scope.target` / 每表 `game_key` 等），再跑同一条命令。

`ls` 核对 `manifest.table_count == inventory_check.table_count` 且每个 `ctas[].file` 存在后，再提交作业。

脚本按 `projections[].type` 选模板：

| type | 过滤方式 |
|---|---|
| `user_table` | JOIN `step1_temp_sampled_users`，追加或保留 `label`，`LIMIT 1 BY` 用户键 |
| `user_keyed` | JOIN `step1_temp_sampled_users`（有用户键的行为/明细表） |
| `game_keyed` | 一行一游戏的本体维表用该表 `projections[].game_key = game_scope.target` 独立过滤，投影后通常 1 行；不复用 cold_start 会扩展的 `sql_fragments.game_filter`。`config.yaml` `projection.catalog_copy` 中的目录维表整表拷贝 |

## 执行流程

读 `step1_4_ctas_manifest.json` 的 `ctas[]`。按 `file` 提交，每条 `submit_resource_job` 用 `command_file="step1_4_sql/<file>"`（`resource_id="clickhouse"`，`task_type="sql_query"`），不要改文件内容。

同一轮最多 **8** 个在途 CTAS，目标表名互不相同。超过 8 张则分批：上一批全部 `collect_job` 完成后再提下一批。

**第 1 轮 submit** — 本批每个 `file` 一次，写在同一条回复里：

```
submit_resource_job(resource_id="clickhouse", task_type="sql_query", command_file="step1_4_sql/00_<table_a>.sql")
submit_resource_job(resource_id="clickhouse", task_type="sql_query", command_file="step1_4_sql/01_<table_b>.sql")
```

**第 2 轮 poll** — 对返回的全部 `job_id` 各发一次 `poll_job(job_id, watch_sec=…, stop_on_terminal=true)`。

**第 3 轮 collect** — 终态后对各 `job_id` 发 `collect_job`。

失败项按报错改 plan / 重跑脚本后，只重提失败表对应的 `command_file`。已成功的表不要重跑。

多条 CTAS 同时读 `step1_temp_sampled_users` 是安全的。

## Gate 与表数

全部 CTAS `collect_job` 成功后：

1. `submit_resource_job(..., command_file="step1_4_sql/step1_4_gate.sql")` → poll → collect。产物表 `step1_temp_step1_4_gate` 一行一张交付表。
2. 读 gate 结果。失败条件：
   - `user_table`：`out_users != sampled_n`
   - `user_keyed`：`out_users > sampled_n`
   - `game_keyed`：不做硬检查
3. `submit_resource_job(..., command_file="step1_4_sql/step1_4_count_check.sql")`。`actual == inventory_check.table_count` 后进入 step1_5。

## 产出

全部表建齐、gate 通过后进入 step1_5。本地过程文件在 `step1_4_sql/`，不算交付产物。
