Required first tool call for every question answered by querying the configured database,
including short and single-table questions. Delegate before any parent `execute`, shell
database client or schema lookup.
Answer natural-language database questions through the configured local SQL service.
Discover actual tables and columns, resolve mappings, generate read-only SQL, execute it,
and correct it using tool evidence. Follows Perceptor, Generator, Validator, Reflector,
Executor and Selector responsibilities within one ReAct loop; calls a bounded database
exploration agent when query context is missing. Supports simple aggregates as well as joins and complex
queries. No semantic service is required. This is a separate implementation from `nl2sql`.
Uses a task-local contract, issue budgets and finish_query for terminal delivery. A blocking
business choice returns needs_clarification; repeating the same task does not resolve it.

Input: the original data question and supplied evidence, definitions, source constraints and
filters. Do not invent table mappings or append a delivery protocol; the reply contract is
already defined. Output: JSON containing status, query_id, executed SQL, observed rows, source,
definitions, assumptions, truncation, artifact paths and unresolved questions. Errors and
missing user choices are returned explicitly. Plugin hooks save actual execution results
and enrich the reply; do not ask this subagent to write or reproduce files. Database scope is determined by the local SQL service’s
tools, not by another plugin's datasource configuration. Do not use this capability for a
different source or for charts, interpretation and analysis of already available results.
