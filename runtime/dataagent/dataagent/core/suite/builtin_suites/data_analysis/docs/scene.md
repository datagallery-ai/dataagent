# 场景绑定

每个场景一份配置，放在 `config/scenes/<scene_name>.yaml`。文件名去掉扩展名就是 `semantic_download` 的 `scene_name`。`materialize_scene.py` 按这个名字把对应文件拷成工作区里的 `scene.yaml`。Skill 和逻辑 SQL 使用占位符，执行时用这份配置里的名字替换。文件中的 `scene_id` 写入采样 plan。

| 配置 | 进入采样后的位置 |
|---|---|
| `match_terms` | 任务里的场景名对不上文件时，用来和任务中的业务词对照 |
| 任务参数 `target_entity` | `entity_scope.target` |
| `entity.tables` | `role_candidates.entity_dim`，投影类型为 `entity_keyed` |
| `entity.key_table`、`entity.key_column` | 目标实体过滤使用的表和列 |
| `sampling.defaults`、`sampling.pos_neg_ratio` | 任务没有给出对应值时采用 |
| `sampling.prelabeled` | 用来判断用户表是否已经带有正负标签 |
| `label_families` | 按 `objective` 选择当前 schema 能够构造的负样本群体 |

`entity.tables` 留空时，step1_0 根据 schema 推断实体表。`key_table` 或 `key_column` 留空时，从实体表主键补齐。每张 `entity_keyed` 表都用 `entity_filter` 保留目标实体的行。

换场景时新增或修改 `config/scenes/` 下的对应文件，内容包括实体、别名、示例字段和 sampling 策略。
