---
name: user-sampling
description: >-
  为游戏推荐场景构造训练样本（pipeline step1）。
  适用场景：需要采样、构造训练样本、定义 y-label、设计正负样本、打标。
disable-model-invocation: true
---

# User Sampling Skill

Pipeline **step1（采样）**：在源表所在 `database` 内生成 `step1_sampled_` 前缀的投影表，**与源业务表一一对应**；行数按采样用户削减，列集与源表一致；用户表额外追加 **`label`** 列。

- **步骤**：`scripts/step1_0` … `step1_6`
- **口径**：`resources/labels.md`、`candidate_pool.md`、`negative_samples.md`
- **硬约束**：见 ../../subagents/sampler.yaml 的 ## 硬约束 小节

---

## Workspace 与 receipt（对齐 subagent_base）

- 本地文件写在当前 job workspace；job workspace 以外的路径只读；完成后由平台发布到只读共享产物区。
- `receipt.json` 顶层仅 `summary` + `artifacts`。
- `artifacts` 条目格式：`{"kind":"file","path":"...","type":"..."}` 或 `{"kind":"clickhouse_table","uri":"clickhouse://<database>/<table>","name":"<database>.<table>"}`。
- 如果某步因为阻塞无法继续（如缺表、表数不够、审核不通过），写阻塞说明并停在此步。

---

## 采样产出原则

- **交付表** `step1_sampled_*`：张数 = 源业务表数（`inventory_check.table_count`，与 `source_table_inventory.tables` 一致）；全表投影，不许跳过；保留源表列集，按采样用户缩行；用户表加 `label`。
- **中间表** `step1_temp_*`：仅用于过程计算的临时表，不计入交付；如 `step1_temp_sampled_users`、`step1_temp_similar_games` 等。
- **step1_0前缀**：先落盘与库一致的 `step1_0_table_schema.json`，再写 `step1_0_sampling_plan.json`；`projections[]` 与 `source_table_inventory.tables` 1:1。

## 步骤一览

| 步骤 | 脚本 | 产出 |
|---|---|---|
| **step1_0** | `scripts/step1_0_sampling_plan.md` | `step1_0_table_schema.json` → `step1_0_sampling_plan.json` |
| **step1_1** | `scripts/step1_1_explore_target_game.md` | 更新 plan 的 `mode`；`step1_temp_positive_probe` |
| **step1_2** | `scripts/step1_2_similar_games.md` | 更新 `game_scope.similar_games`（cold_start）；`step1_temp_similar_games` |
| **step1_3** | `scripts/step1_3_build_training_set.md` | `step1_temp_sampled_users` |
| **step1_4** | `scripts/step1_4_project_tables.md` | `step1_sampled_<源表名>` |
| **step1_5** | `scripts/step1_5_stats.md` | `step1_output_meta.json` |
| **step1_6** | `scripts/step1_6_finalize.md` | `receipt.json` |

step1_0 创建 `step1_0_sampling_plan.json`；step1_1 / step1_2 在其上追加字段；step1_6 写 `receipt.json`。

**cold_start**：step1_1 正样本 &lt; 500 → step1_2；`similar_games` 为空时在 plan 内记 fallback。

---

## 交付物（摘要）

| 类型 | 命名 | 说明 |
|---|---|---|
| 表结构文件 | `step1_0_table_schema.json` | 语义服务查库结果，下游查列信息就靠它 |
| 采样计划 | `step1_0_sampling_plan.json` | step1_0 创建，step1_1 / step1_2 追加字段 |
| 交付投影表 | `step1_sampled_<源表名>` | 与源表一对一，行数缩到采样用户，用户表多一列 `label` |
| 统计元信息 | `step1_output_meta.json` | step1_5 生成，含用户数、label 分布、表数核对等 |
| 定稿凭证 | `receipt.json` | step1_6 生成，`artifacts` 中登记 `step1_output_meta.json`（file）+ 全部 `step1_sampled_*`（clickhouse_table） |
| 过程临时表 | `step1_temp_*` | 不计入交付，用完可删 |

详情见 `scripts/step1_6_finalize.md` §3。

---

## 输入 JSON（参考）

```json
{
  "objective": "<优化目标>",
  "target_game": "<目标游戏>",
  "database": "<目标库>",
  "T0": "<ISO 日期>",
  "label_window_days": 90,
  "sample_size": null,
  "run_id": "<objective缩写_游戏短名_yyyymmdd>",
  "constraints": {"exclude_converted": true}
}
```
