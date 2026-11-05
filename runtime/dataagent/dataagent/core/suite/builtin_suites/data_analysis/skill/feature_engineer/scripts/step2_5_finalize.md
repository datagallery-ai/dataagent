# step2_5: finalize（定稿）

验收通过后由脚本生成 **`receipt.json`** 并跑完成自检。本步不写 SQL、不连 `<warehouse>`。

## 唯一入口（强制）

```bash
python skill/feature-engineer/scripts/step2_5_driver.py finalize --out-dir .
```

**禁止**直接调用下列脚本绕过入口（参数容易漏填、三步语义会被拆散）：

- `step2_5_receipt.py`
- `step2_verify_all.py`
- `step2_2_validate_deployment_contract.py`

排障时仍走驱动子命令（入口一致）：

```bash
python skill/feature-engineer/scripts/step2_5_driver.py validate-contract --out-dir . [--cleaning-report step2_3_cleaning_report.json] [--enforce]
python skill/feature-engineer/scripts/step2_5_driver.py receipt --out-dir .
python skill/feature-engineer/scripts/step2_5_driver.py verify-all --out-dir .
```

`finalize` 固定按顺序执行三步，**每步都会跑完**，任一步失败不中断后两步，最后汇总失败步骤并以非 0 退出：

| 顺序 | 子命令 | 语义 |
|------|--------|------|
| 1 | `validate-contract` | 结构契约校验；自动预填契约 / schema / wide CSV / expanded SQL；`--cleaning-report` **仅显式传入时前传** |
| 2 | `receipt` | 从既有产物确定性生成 `receipt.json`（`summary` + `artifacts`），**禁止手写 receipt** |
| 3 | `verify-all` | 经驱动调用 `step2_verify_all.py --require-receipt`；`--report-json` 仅显式传入时前传 |

也可单独跑子命令做排障，但定稿交付必须以 `finalize` 为准。

### `--cleaning-report` 缺参处理

`--cleaning-report` **不会**因磁盘上已有 `step2_3_cleaning_report.json` 而自动附加。

- 未显式传入时：契约校验仍执行，但 DROP 解释为空集（无法用 cleaning-report 解释「契约特征不在最终 CSV」）。
- `validate-contract --enforce` **缺** `--cleaning-report` 且校验需要 DROP 对账时报错时：重跑 finalize 并补参：

```bash
python skill/feature-engineer/scripts/step2_5_driver.py finalize \
  --out-dir . --cleaning-report step2_3_cleaning_report.json --enforce
```

需要 DROP 对账时应先确保 step2_3 gate 已写出该文件。

## receipt 字段来源

- 行数 / 列数 ← `step2_4_wide_userfiltered.csv`（列数取表头）
- 表数 ← `schema_resolution.json` 的 `source_tables`
- 正负样本比 ← `step2_4_user_filter_report.json` 的 `n_pos` / `n_neg`
- 契约条目 ← 仅当 `validation.structural_validation.passed=true` 时登记（否则省略并在 stderr 说明）

## 前置

| 前置条件 | 缺失时脚本行为 |
|---------|---------------|
| `schema_resolution.json` | 报障退出 |
| `step2_4_wide_userfiltered.csv`（非空） | 报障退出 |
| `step2_2_feature_derivation.md` | 报障退出 |
| `step2_4_user_filter_report.json`（含 `n_pos`/`n_neg`） | 报障退出 |
| `step2_2_deployment_feature_contract.json`（可选） | 未通过结构检查则省略该条目，其余不受影响 |

`artifacts` 登记下列交付物（`path` 为工作区内相对路径，不要写 `workflow_outputs/`——归档由 workflow 做）：

| kind | path | type |
|------|------|------|
| `file` | `schema_resolution.json` | `json` |
| `file` | `step2_2_feature_derivation.md` | `markdown` |
| `file` | `step2_2_deployment_feature_contract.json` | `json`（仅结构检查通过时） |
| `file` | `step2_4_wide_userfiltered.csv` | `csv` |

`summary` 为非空字符串，含表数 / 行数 / 列数 / 正负样本比 / 库名（示例：

```json
{
  "summary": "特征工程完成：基于 25 张交付表，产出 9316 行 107 列宽表。正 1894 : 负 7576 = 1:4，库 <output_database>",
  "artifacts": [
    {"kind": "file", "path": "schema_resolution.json", "type": "json"},
    {"kind": "file", "path": "step2_2_feature_derivation.md", "type": "markdown"},
    {"kind": "file", "path": "step2_2_deployment_feature_contract.json", "type": "json"},
    {"kind": "file", "path": "step2_4_wide_userfiltered.csv", "type": "csv"}
  ]
}
```
