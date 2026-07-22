# step2_5: finalize（定稿）

**目的**：验收通过后写 **`receipt.json`** 交给 workflow。本步不写 SQL、不连 ClickHouse。

## 前置

| 前置条件 | 检查要点 |
|---------|---------|
| `schema_resolution.json` | 已创建，`source_tables` 与 `output_meta.projection_tables` 一致 |
| `step2_4_wide_userfiltered.csv` | 已导出到当前 job workspace，非空 |

任一不满足禁止写 receipt。

## 写 receipt.json

验收通过后写入当前 job workspace。`receipt.json` 仅顶层 `summary` + `artifacts`。

`artifacts` 仅含 Model Training 下游所需的两个文件：

| kind | path | type |
|------|------|------|
| `file` | `schema_resolution.json` | `json` |
| `file` | `step2_4_wide_userfiltered.csv` | `csv` |

`path` 必须是当前 job workspace 内的相对路径，不要写到 `workflow_outputs/`（归档由 workflow 做）。

### 示例

```json
{
  "summary": "特征工程完成：基于 25 张交付表，产出 9316 行 107 列宽表。正 1894 : 负 7576 = 1:4，库 game_mock_syw",
  "artifacts": [
    {"kind": "file", "path": "schema_resolution.json", "type": "json"},
    {"kind": "file", "path": "step2_4_wide_userfiltered.csv", "type": "csv"}
  ]
}
```

`summary` 必须为非空字符串，包含关键统计：表数、行数、列数、正负样本比、库名。