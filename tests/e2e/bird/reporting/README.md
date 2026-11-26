# 从 BIRD 运行归档生成报告

在有原始题库、SQLite 数据库和逐题运行目录的机器上，从仓库根目录运行：

```sh
uv run python tests/e2e/bird/reporting/build_report.py \
  --run-dir "$PART_ONE" --run-dir "$PART_TWO" \
  --dataset "$DATASET_ROOT/dataset/dev.json" \
  --databases "$DATASET_ROOT/dataset/databases" \
  --target-slots 9 --output "$REPORT_DIR"
```

每个运行目录应含 `cases/<db_id>__q<question_id>/stats.json`。按从早到晚的顺序传各分区和补跑目录；同题用最后一个有终态的版本。输出目录必须为新目录。只汇总所给目录中有终态记录的题目，部分运行报告的分母随之变化。使用运行时的数据库快照和题库；脚本逐题检查题干、evidence、题号及库名。有 Gold SHA 的输入会校验标准 SQL，较早归档缺少该字段时会用指定题库对最终 SQL 重新评分。

`four-section/report.md` 包含统计范围、Generation/AfterValidation 准确率、Generator 槽位产出及重试、逐 checker 修复效果。Generation 在各初始候选的**完整执行结果**上投票。AfterValidation 使用最终选中 SQL 的评分；两者均报告严格匹配和允许额外列。评分使用只读 SQLite，候选及 Gold 执行超时会留下缺失标签。记录不全时查看覆盖提示，尤其不要把 NA 当成零。中间产物的 SQL 哈希、路径和逐题结果适用于受控的运行归档。

- `effective-inventory.json` 记录每题的终态版本；`scored-percase.jsonl` 保存与该版本绑定的候选评分。
- `audits/` 记录 Generator 首次成功槽、内部重试补回槽和可还原的 checker 输入/实际采用输出。一次修复中的多次解析或请求分别计数。
- 观测事件取自 `observation-v1/events.jsonl`；评分前后 SQL 所需的引用指向对应题目的 `observation-v1/blobs/`。如果缺少完整的节点或事件记录，报告列出覆盖缺口。
- `four-section/verification.json` 记录报告输入文件哈希。保留原始 SQLite 快照以供复核。

已有冻结的清单和已评分逐题标签时，也可单独运行以下纯渲染入口：

```sh
uv run python tests/e2e/bird/reporting/render_report.py \
  --inventory "$REPORT_DIR/effective-inventory.json" \
  --dataset "$DATASET_ROOT/dataset/dev.json" \
  --labels "$REPORT_DIR/scored-percase.jsonl" \
  --target-slots 9 --stages Generation,AfterValidation \
  --generator-audit "$REPORT_DIR/audits/generator-summary.json" \
  --checker-audit "$REPORT_DIR/audits/checker-summary.json" \
  --whole-chain-audit "$REPORT_DIR/audits/whole-chain.json" \
  --output "$RENDER_DIR"
```

只评测 Generator 的历史档案可以使用 `--stages Generation`，三项 audit 也可省略。渲染入口只读 JSON，不需要模型或数据库服务。
