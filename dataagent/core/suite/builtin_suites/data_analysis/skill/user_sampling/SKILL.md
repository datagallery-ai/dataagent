---
name: user_sampling
description: >-
  为游戏推荐场景构造训练样本（pipeline step1）。
  适用场景：需要采样、构造训练样本、定义 y-label、设计正负样本、打标。
disable-model-invocation: true
---

# User Sampling Skill

## 必填参数

<必须>以下参数由上游（workflow coordinator）明确传入，**禁止自行猜测或随机选取**。若缺失任一必填项，立即停止并在 receipt 中写阻塞原因。</必须>

| 参数 | 必填 | 说明 |
|------|------|------|
| `source_database` | **是** | 源数据所在 ClickHouse 库，只读。通常从语义检索或任务参数获取 |
| `output_database` | **是** | 产物表写入的目标 ClickHouse 库。由调用方指定，**严禁**从数据库列表中任意挑选空库代替 |
| `target_game` | 任务参数 |
| `run_id` | 任务参数 |
| `sample_size` | 任务参数（默认 null） |
| `cold_start_threshold` | 默认 500 |

> 不同表中的 `usid`、`rank_flg`、`dsid` 均为用户 ID 等效列。等效映射见 `step1_0_table_schema.json` 的 `column_aliases.user_id_columns`。

## 模式判定

step1_0 产出的 schema 每表 `columns` 非空（表名齐 ≠ 结构齐），之后按 `step1_0_sampling_plan.md` §2 执行 SQL 判定：label 列存在且 0/1 双侧各有 ≥1 人则 `prelabeled`，否则 `regular`（正样本 < 500 则 `cold_start`）。

- `prelabeled`：跳过 step1_1、step1_2，余同主路径
- `regular`：按事件口径判定正负样本

## 步骤一览

<必须>所有 ClickHouse SQL **仅**通过 `submit_resource_job`（`resource_id="clickhouse"`）执行。</必须>

硬约束见 `../../subagents/sampler.yaml` 的 ## 硬约束 小节。

| 步骤 | 脚本 | 产出 | 跳过条件 |
|---|---|---|---|
| **step1_0** | `scripts/step1_0_sampling_plan.md` | `step1_0_table_schema.json` → `step1_0_sampling_plan.json` | <必须>不跳过 |
| **step1_1** | `scripts/step1_1_explore_target_game.md` | `step1_temp_positive_probe`，更新 plan `mode` | `mode=="prelabeled"` |
| **step1_2** | `scripts/step1_2_similar_games.md` | `step1_temp_similar_games`，更新 `game_scope.similar_games` | `mode=="prelabeled"` 或 `mode=="regular"` |
| **step1_3** | `scripts/step1_3_build_training_set.md` | `step1_temp_sampled_users` | <必须>不跳过（prelabeled 走内置分支） |
| **step1_4** | `scripts/step1_4_project_tables.md` | `<源表名>`（output_database 内，与源表同名） | <必须>不跳过 |
| **step1_5** | `scripts/step1_5_stats.md` | `step1_output_meta.json` | <必须>不跳过 |
| **step1_6** | `scripts/step1_6_finalize.md` | `receipt.json` | <必须>不跳过 |

**cold_start**：step1_1 正样本 < 500 → step1_2；`similar_games` 为空时在 plan 内记 fallback。

## 采样产出原则

- **交付表**<必须>表名与源表同名，写于 `output_database`，一张都不能少</必须>；行数按采样用户削减；保留源表列集；用户表含 `label`（event_derived 追加 / prelabeled 保留源列）。
- **中间表** `step1_temp_*`：仅用于过程计算，不计入交付。
- **step1_0**：<必须>`step1_0_table_schema.json` 与 CH 清单 1:1，且每表 `columns` 非空，方可落盘</必须>（表名齐 ≠ 结构齐）；之后写 `step1_0_sampling_plan.json`，`projections[]` 与清单 1:1。

## receipt 格式

`receipt.json` 顶层仅 `summary` + `artifacts`。条目格式：
- 文件：`{"kind":"file","path":"...","type":"..."}`
- ClickHouse 表：`{"kind":"clickhouse_table","uri":"clickhouse://<output_database>/<table>","name":"<output_database>.<table>"}`

如果某步阻塞无法继续（缺表、表数不够、审核不通过），写阻塞说明并停在此步。

## 交付物（摘要）

| 类型 | 命名 | 说明 |
|---|---|---|
| 表结构文件 | `step1_0_table_schema.json` | 含 `tables`（每表 columns 非空）+ `join_hints` + `role_candidates` + `column_aliases` |
| 采样计划 | `step1_0_sampling_plan.json` | step1_0 创建，后续各步读取 |
| 交付投影表 | `<源表名>`（output_database 内，与源表同名） | 与源表一对一，用户表含 `label` |
| 统计元信息 | `step1_output_meta.json` | 用户数、label 分布、表数核对 |
| 定稿凭证 | `receipt.json` | `artifacts` 登记 `step1_output_meta.json`（file）+ 全部交付表（clickhouse_table） |
| 过程临时表 | `step1_temp_*` | 不计入交付，用完可删 |

## 输入 JSON（参考）

```json
{
  "objective": "<优化目标>",
  "target_game": "<目标游戏>",
  "source_database": "<源数据所在库>",
  "output_database": "<产表所在库>",
  "T0": "<ISO 日期>",
  "label_window_days": "<天数>",
  "lookback_days": "<天数>",
  "sample_size": null,
  "run_id": "<objective缩写_游戏短名_yyyymmdd>",
  "constraints": {"exclude_converted": true}
}
```
