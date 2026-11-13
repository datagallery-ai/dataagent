## bio_lab evaluation scenario

For database, data analysis, and multi-step tasks:
1. First call `data-qa__get_ontology_description_tool` to understand the schema.
2. Use native `write_todos` to create a concrete work plan before execution; update todo statuses as steps finish.
3. For database queries, delegate to `task(subagent_type="nl2sql")`. For experiment creation, follow the installed bio_lab skills and their local scripts. Their database is the isolated `USER_SQLITE_PATH` fixture; do not invent other database paths or contact business write services.
4. Whenever clarification or confirmation is needed, actually call `data-qa__request_human_feedback`. The old skill name `request_human_feedback` refers to this tool. Do not replace this call with a question in ordinary prose. Do not assume a request to create an experiment is approval of its final selected samples.
5. Follow the skill's sample checks and final confirmation before running its insertion script. A rejected confirmation forbids that write. Scripts run using the current Python environment; the installed skill directory contains their relative `scripts/` paths.
6. Finish with the complete requested result. Do not claim success solely because a tool was called; use its actual result.

Simple non-database questions need no schema lookup or plan. No expected case answers are included in this scenario.
