对于涉及数据库、数据分析或多步骤工作流的复杂任务，按以下顺序执行：

1. 先调用 `data-qa__get_ontology_description_tool` 了解数据库 schema 与可用表。
2. 调用 `write_todos` 制定工作计划，再开始实质性执行。
3. 每完成一个 todo，调用 `write_todos` 更新完成状态，避免重复执行已完成的步骤。
4. 数据库查询和分析中的 SQL 任务必须通过 `task(subagent_type="nl2sql", description="完整任务描述")` 委派；
5. 最后不调用工具的轮次，完整返回用户要求的信息；
