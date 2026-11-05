# Step2_4 最终宽表 CSV 导出

> ⛔ **独立 todo，不可合并。** `gate_data_verified_2_4.txt` 存在前禁止进入。
> 分批 SELECT、OFFSET 与 CSV 组装由 `step2_4_driver.py` 生成，**禁止手写**。

## 顺序（不可颠倒）

```bash
# gate 通过后：按 step2_4_run.json 的 export 命令生成批次
python skill/feature-engineer/scripts/step2_4_driver.py export-plan \
  --raw-count raw_step2_4_count.json --batch-size 5000 --out-dir .
# 按 jobs 逐条 submit_resource_job → poll_job → collect_job（payload 按 collect_as 落盘）
# 再跑清单 after_collect：
python skill/feature-engineer/scripts/step2_4_driver.py append-csv \
  --raw-batch raw_step2_4_export_batch_0001.json [...] --out-dir .
```

> **兼容两种落盘格式**：`append-csv` 同时接受 `{columns, rows}` 块与 `save-collect --extract-rows` 落盘的**扁平 rows 列表**（列名取首条记录键），不再报「没有 (columns, rows) 结果」，也无需手工 python3 拼 CSV。落盘校验可用 `step2_4_driver.py check-collect --manifest step2_4_run.json [--expect-rows]`。

写出非空 `step2_4_wide_userfiltered.csv` 后才能做契约校验 / step2_5。

## 导出方式

**默认**：MCP 分批 `SELECT`（不需要密码，**不要** `curl` 直连）。
**可选直连**：仅当 `test -n "${WAREHOUSE_PASSWORD:-${CLICKHOUSE_PASSWORD:-}}"` 通过时允许**一次**只读
`SELECT * FROM {{output_database}}.step2_4_wide_userfiltered`。首次认证失败（401/403、`Authentication failed`）→ 立即回分批导出，禁止第二次直连。连接参数只读已预置环境变量，密码无缺省。

## 严禁

扫描配置/进程环境/datasource 挖凭据；暴力猜密码；为找凭据连续 bash；直连源表或 step2_0–step2_3 表；用 Python 连 `<warehouse>`。

## 收尾

truncator 把结果落到 `tool-results/*.txt` 时，先按该文件落成 `collect_as` 同名 payload 再 `append-csv`。
表头不得含地理原列（只保留 `{col}_tier`）——由随后的 `step2_2_validate_deployment_contract.py` 拦截，不必再单独跑 `--check-csv`。
