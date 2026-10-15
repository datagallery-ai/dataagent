# step1_0：schema 与采样计划

本步产出两份文件：`step1_0_table_schema.json` 和 `step1_0_sampling_plan.json`。后续步骤只读这两份文件和 scene 里已经固化进 plan 的值。

## 1. 读取配置并核对任务参数

先按 SKILL.md 的顺序确定 `scene_name`：任务里的 `scene_name` 或 `scene` 能对上文件就用它；对不上时，用任务里的业务词对照 `config/scenes/` 中每份文件的文件名、`scene_id`、`entity.label` 和 `match_terms`，只保留唯一对得上的那份。没有唯一结果时停止。然后运行：

```bash
python skill/user_sampling/scripts/materialize_scene.py --scene-name <scene_name>
```

脚本把 `config/scenes/<scene_name>.yaml` 拷成工作区 `scene.yaml`，并把 `config/data_source.md` 拷进工作区。然后读取这两份工作区文件。`semantic_download` 使用同一个文件名。`scene.yaml` 里的 `scene_id` 写入 plan，不传给语义服务。

按 SKILL.md 的输入表核对任务参数。`sample_size`、时间窗和正负比按「任务值优先，scene 默认值其次」解析，并把最终数字写入 plan。`target_entity` 写入 `entity_scope.target`。

## 2. 列出源库表并生成 schema

先按 `data_source.md` 的 catalog 占位符列出源库表名，写入 `step1_source_tables.json`：

```sql
SELECT <table_name_expr> AS table_name
FROM <catalog_tables>
WHERE <database_expr> = '{{source_database}}'
ORDER BY table_name
```

表清单落盘后，用上面同一个 `scene_name` 调用一次 `semantic_download(scene_name=<scene_name>)`，再用回执路径生成 schema：

```bash
python skill/user_sampling/scripts/build_table_schema.py \
  --dump <receipt-path> \
  --source-database {{source_database}} \
  --inventory-file step1_source_tables.json \
  --out step1_0_table_schema.json
```

脚本退出码为 2 时，按 stderr 里的缺列表查询 `<catalog_columns>`，把结果写入 `step1_source_columns.json`，加上 `--columns-file` 再跑一次。

`semantic_download` 失败时，改查源库全部表的列清单。当前 `data_source.md` 把 `<catalog_columns>` 映射到 ClickHouse 的 `system.columns`。结果写入 `step1_source_columns.json`，字段为 `table_name`、`column_name`、`data_type`：

```sql
SELECT
  <table_name_expr> AS table_name,
  <column_name_expr> AS column_name,
  <data_type_expr> AS data_type
FROM <catalog_columns>
WHERE <database_expr> = '{{source_database}}'
  AND <table_name_expr> IN (<step1_source_tables.json 中的表名>)
ORDER BY table_name, <column_position_expr>
```

然后不带 `--dump` 生成 schema。产物字段与下载成功时相同：

```bash
python skill/user_sampling/scripts/build_table_schema.py \
  --source-database {{source_database}} \
  --inventory-file step1_source_tables.json \
  --columns-file step1_source_columns.json \
  --out step1_0_table_schema.json
```

这条路径里，列的 `description` 为空，`isPrimaryKey` 为 false，`join_hints` 为空数组。`valueType` 保留仓里的原文。

schema 通过的条件：

- `table_names` 与 `step1_source_tables.json` 一一对应。
- 每张表至少有一列。
- `valueType` 和 `description` 保持脚本输出的原文。

然后在这份 schema 上补齐五个角色和用户键别名：`user_table`、`label_event`、`activity_event`、`conversion_event`、`entity_dim`，以及 `column_aliases.user_id_columns`。`scene.yaml` 的 `entity.tables` 有值时，原样写入 `entity_dim`，并确认这些表都在源库清单里。`entity.tables` 为空时，按表名、列名和 description 推断实体表。

## 3. 判定 mode 并写出实体过滤

用户表含有 `sampling.prelabeled.label_column_aliases` 命中的列时，统计该列上配置正值和负值各自的人数。两侧人数都大于 0，mode 记为 `prelabeled`，并把这两个取值写入 `label_values`。没有命中列，或某一侧人数为 0，mode 记为 `regular`。

实体键按这个顺序确定：`scene.entity.key_table` 和 `scene.entity.key_column` 都有值时用这两项；只给了一项时，用给出的那一项，另一项从实体表主键补齐；两项都空时，从 `entity_dim` 里选出能对应 `target_entity` 的主键列。比较值是否加引号，看该列在 schema 里的 `valueType`。

据此写入：

- `sql_fragments.entity_key_expr`：实体键列名。
- `sql_fragments.entity_filter`：实体键非空，并且等于 `entity_scope.target`。
- `keys.entity_key_default`：上面选出的列名。
- `sampling_sources.entity_dim`：提供这个键的那张表。

## 4. 写采样计划

`step1_0_sampling_plan.json` 包含这些字段：

- `scene_id`、`source_database`、`output_database`、`run_id`、`T0`、时间窗、`sample_size`、`mode`
- `entity_scope`，其中 `target` 等于任务里的 `target_entity`
- `sampling_config`，含正负比、匹配到的 label family 和负样本目录
- `sampling_sources`、`keys`、`label_values`、`sql_fragments`
- `source_table_inventory`、`inventory_check`
- 与每张源表一一对应的 `projections`

投影类型按下面规则标注，一张表只标一种：

| type | 适用表 |
|---|---|
| `user_table` | 采样用的那张用户主表 |
| `user_keyed` | 其余含用户键、且不是实体维表的表 |
| `entity_keyed` | `entity_dim` 里除用户主表以外的表 |

`regular` 用 `objective` 匹配 `scene.sampling.label_families`。命中的 family 写入 plan；负样本只保留这个 family 声明、并且当前 schema 能够构造的群体。正样本谓词来自事件表在 label 窗口内的真实取值分布。`prelabeled` 记录用户表上的标签列和正负取值，事件表相关字段留空。

写完后核对 `inventory_check.ok` 为 true，并且投影张数等于源表张数。然后读取 `scripts/step1_1_build_training_set.md`。
