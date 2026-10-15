---
name: user_sampling
description: >-
  为任意实体圈选场景构造训练样本（pipeline step1）。
  适用场景：需要采样、构造训练样本、定义 y-label、设计正负样本、打标。
disable-model-invocation: true
---

# User Sampling Skill

为任意实体圈选构造训练样本和一份独立测试集。表名、列名、实体范围和采样策略来自工作区里的 `scene.yaml`。仓名、catalog 和 receipt 协议来自工作区里的 `data_source.md`。step1_0 把这两份配置和语义结果收成 schema 与 plan；语义下载失败时，用仓里的列清单生成同一份 schema。后面各步只读 plan。

步骤里的 SQL 是逻辑 SQL。提交前按 `resources/logical_sql.md` 换成当前仓方言，再通过 `data_source.md` 里的 `<resource_id>` 提交。

## 输入

任务文本给出这些字段后才能开始：

| 参数 | 是否必填 | 用法 |
|---|---|---|
| `source_database` | 必填 | 只读源库 |
| `output_database` | 必填 | 产物库 |
| `scene_name` | 必填 | 按下面的顺序从任务文本选出 `config/scenes/<scene_name>.yaml`，并传给 `semantic_download` |
| `objective` | 必填 | 要优化的行为，用来匹配标签族 |
| `target_entity` | 必填 | 要圈选的实体，写入 `entity_scope.target` |
| `run_id` | 必填 | 本次运行标识 |
| `T0` | 必填 | 标签切分基准日期 |
| `sample_size`、`lookback_days`、`label_window_days` | 可选 | 任务里有就用任务值，没有就用 `scene.yaml` 的 `sampling.defaults` |
| `pos_neg_ratio` 或 `neg_per_pos` | 可选 | 任务里有就用任务值，没有就用 `scene.yaml` 的 `sampling.pos_neg_ratio` |

`scene_name` 按这个顺序确定：

1. 任务文本里的 `scene_name` 或 `scene` 能对上 `config/scenes/<名字>.yaml` 时，用这个名字。
2. 对不上时，列出 `config/scenes/` 里的 yaml。用任务里的业务词去对每份文件的文件名、`scene_id`、`entity.label` 和 `match_terms`。只有一份明显对得上时，用它的文件名。
3. 一份都对不上，或有两份以上都像，写出阻塞说明并停在 step1_0。

其余必填项缺失时同样停在 step1_0。数量、窗口和正负比解析后都要是有效正数，容差要落在 `[0, 1)`。

mode 由 step1_0 判定，结果写入 plan：

- `prelabeled`：用户表已有 scene 配置的标签列，而且正、负两侧都有人。
- `regular`：按目标实体在 label 窗口内的行为构造正负样本。

## 顺序

step1_0 一开始用上面选出的文件名运行 `python skill/user_sampling/scripts/materialize_scene.py --scene-name <scene_name>`。脚本按这个名字把对应 yaml 拷成工作区的 `scene.yaml`，并拷入 `data_source.md`。`semantic_download` 使用同一个文件名。文件里的 `scene_id` 只写入 plan。

| 步骤 | 文档 | 产物 |
|---|---|---|
| step1_0 | `scripts/step1_0_sampling_plan.md` | `step1_0_table_schema.json`、`step1_0_sampling_plan.json` |
| step1_1 | `scripts/step1_1_build_training_set.md` | `step1_temp_sampled_pool`。上限是 `sample_size + floor(sample_size / 4)` |
| step1_2 | `scripts/step1_2_build_test_set.md` | `step1_temp_sampled_users`（原配额）、`step1_test_users`（多出的部分） |
| step1_3 | `scripts/step1_3_project_tables.md` | 每张源表在产物库中的同名表，只用训练集过滤 |
| step1_4 | `scripts/step1_4_stats.md` | `step1_sample_stats.json` |
| step1_5 | `scripts/step1_5_finalize.md` | `step1_output_meta.json`、`receipt.json` |

## 交付

投影类型有三种：`user_table`、`user_keyed`、`entity_keyed`。一张表只标一种。`entity_keyed` 用 plan 里的 `entity_filter` 保留目标实体的行。

训练投影读取 `step1_temp_sampled_users`。`step1_test_users` 保存 `user_key` 和 `label`，并单独登记进 receipt。

`step1_sample_stats.json` 记录查询得到的 `test_set_stats.train_overlap`。定稿条件是这个值为 0。

receipt 登记 `step1_output_meta.json`、`step1_sample_stats.json`、`step1_test_users` 和全部同名交付表。表产物的 kind 和 URI 按 `data_source.md` 的 `<table_kind>`、`<uri_scheme>` 填写。
