# BIRD 端到端评测

[run_eval.py](run_eval.py) 按题绑定 SQLite 和语义服务数据库，用 [BIRD 工作流 YAML](../../../examples/nl2sql-bird-workflow.yaml) 运行 Perceptor → Generator → Bird Validator → Selector，再将最终 SQL 与该题标准 SQL 的执行结果比较。

## 准备数据与环境

从仓库根目录运行 `uv sync --locked`。准备以下本地输入：

```text
bird-data/
  dev.json
  database-map.json
  databases/
    formula_1/
      formula_1.sqlite
```

`dev.json` 是 JSON 数组。以下是用于说明字段的示例，执行时换成与所选 SQLite 数据库匹配的真实题目：

```json
[
  {
    "question_id": 10,
    "db_id": "formula_1",
    "question": "有多少场比赛？",
    "evidence": "每场比赛按唯一的比赛编号计数。",
    "SQL": "SELECT COUNT(DISTINCT raceId) FROM races"
  }
]
```

`question_id` 在题集内唯一。`db_id` 对应 `databases/<db_id>/<db_id>.sqlite`。`database-map.json` 将相同的 `db_id` 映射到语义服务中已注册的数据库名，例如 `{"formula_1":"formula_1"}`；如果注册名不同，将右侧值改成实际名称。脚本逐题传入 question 和 evidence，标准 SQL 仅供评测器评分。

使用示例 YAML 时，通过当前进程环境或 `--env-file` 提供以下变量。环境文件使用 `名称=值` 格式，保存在仓库外。

| 变量 | 用途 |
| --- | --- |
| `BIRD_EVAL_BASE_URL` | OpenAI 兼容模型服务地址 |
| `BIRD_EVAL_API_KEY` | 对应模型服务的凭据 |
| `BIRD_SEMANTIC_BASE_URL` | 已注册 BIRD 数据库的语义服务地址 |

脚本根据每题的数据库映射和 SQLite 文件生成配置。对模型、八个 checker、Selector 或语义超时的实验性改动可写到单独的 YAML 副本，并用 `--config` 指定。分支示例使用 GLM-5.3、low 思考、温度 0.3、单次输出上限 18000、每题模型调用预算 256。

## 先预检，再运行

从仓库根目录执行。将前三行改为自己的绝对路径，预检与正式评测分别使用新的输出目录。

```bash
BIRD_DATA=/absolute/path/to/bird-data
BIRD_ENV=/absolute/path/to/model.env
BIRD_OUTPUT=/absolute/path/to/bird-runs

uv run python tests/e2e/bird/run_eval.py \
  --dataset "$BIRD_DATA/dev.json" --databases "$BIRD_DATA/databases" \
  --database-map "$BIRD_DATA/database-map.json" --env-file "$BIRD_ENV" \
  --config examples/nl2sql-bird-workflow.yaml \
  --run-dir "$BIRD_OUTPUT/preflight-01" --preflight-only

uv run python tests/e2e/bird/run_eval.py \
  --dataset "$BIRD_DATA/dev.json" --databases "$BIRD_DATA/databases" \
  --database-map "$BIRD_DATA/database-map.json" --env-file "$BIRD_ENV" \
  --config examples/nl2sql-bird-workflow.yaml \
  --run-dir "$BIRD_OUTPUT/main-01" --case-workers 2
```

预检用于离线检查输入和配置。真实模型、语义服务连通性和逐题得分以正式运行的记录为准。省略 `--plan` 时运行所给题集。做修订版对比时，固定对应版本的题面、evidence、标准 SQL、数据库和配置，分别保存输出。

## 分区与并发

对包含题号 10、20、30 的题集，可将下列 JSON 保存为 `plan.json`。每个分区先处理 fast，再处理 slow。具体题号须来自实际题集，并确保各分区没有交叉。

```json
{
  "parts": [
    {"part": 1, "fast": [10], "slow": []},
    {"part": 2, "fast": [20], "slow": []},
    {"part": 3, "fast": [], "slow": [30]}
  ]
}
```

在正式命令上加 `--plan "$BIRD_DATA/plan.json" --part 1` 即只运行第一分区。第二、三分区分别用 `--part 2`、`--part 3`，各用独立的 `--run-dir` 和对应的服务凭据。一个进程执行一个分区。`--case-workers` 控制同时在跑的题数；单题内部可同时请求多个候选，题并发与模型请求并发是两个指标。若多个进程使用同一模型凭据，传入同一 `--admission-dir`，让其共用模型请求限流账本。

## 看进度和结果

运行期间可用 `tail -f "$BIRD_OUTPUT/main-01/run.log"` 查看逐题进度，并读取下列文件：

| 位置（相对于本次输出目录） | 用途 |
| --- | --- |
| `manifest.json` | 本次题集、分区、配置和并发等运行信息 |
| `summary.json`、`progress.json` | 完成数、错误数、严格及允许额外列的正确数与准确率 |
| `cases/<db_id>__q<question_id>/stats.json` | 该题错误、耗时、调用数以及两种评分 |
| `cases/<db_id>__q<question_id>/predicted.sql` | Selector 最终选出的 SQL |
| `cases/<db_id>__q<question_id>/model-events.jsonl` | 该题的逻辑模型请求、响应和错误事件 |

严格评分比较 SQLite 查询返回的行集合，忽略顺序和重复行。允许额外列的口径还接受这样的结果：预测 SQL 多返回了列，但能从中选出固定的列组合，使投影后的行集合与标准答案一致。两个指标都对同一条最终 SQL 评分，因此该开关不会改变 Selector 的投票。跨分区汇总时先相加正确题数和题数，再计算比例。读阶段准确率时同时看已完成题数和错误数。

## 中断与重跑

保留上一批目录。沿用原命令和分区，将输出目录改为新的 `--run-dir`，再加 `--retained-root "$BIRD_OUTPUT/main-01"`。脚本校验题集、YAML 和数据库名映射文件的 SHA‑256，复用分区内已完成且无运行或评分错误的题，继续处理其余题。也可将待补题号写进独立分区计划再运行，合并时按题号采用最新一轮结果。成功但答错属于有效结果。切换题面、evidence、标准答案或配置的实验，应独立生成报告并记录对应版本。

## 生成四节分析报告

评测完成后，用运行目录、对应版本的题集和数据库离线生成数据集、阶段准确率、Generator 重试与缺槽、checker 修复效果四节报告：

```bash
uv run python tests/e2e/bird/reporting/build_report.py \
  --run-dir "$BIRD_OUTPUT/main-01" --dataset "$BIRD_DATA/dev.json" \
  --databases "$BIRD_DATA/databases" --output "$BIRD_OUTPUT/report"
```

读取 `$BIRD_OUTPUT/report/four-section/report.md` 查看结果。跨分区或合并整题补跑时，重复传入 `--run-dir`，按从旧到新排列；对同一题号取最新终态。已持有冻结的逐题清单、评分标签及可选的 Generator/checker 审计文件时，也可直接使用 `reporting/render_report.py`。详细输入格式见 [报告输入指南](reporting/README.md)。
