# 数据源

占位符和当前取值写在 `config/data_source.md`。采样开始时，`materialize_scene.py` 把这份文件复制到任务工作区，后续步骤读取工作区副本。

采样子代理按副本里的 `<resource_id>` 提交 SQL，按 `resources/logical_sql.md` 把逻辑算子编译成当前仓方言。receipt 里的表产物使用 `<table_kind>`，地址写成 `<uri_scheme>://<output_database>/<table>`。

换仓时修改 `config/data_source.md` 的「当前取值」列，以及元数据表对照表里对应的那一列。`{{source_database}}` 和 `{{output_database}}` 来自任务参数。
