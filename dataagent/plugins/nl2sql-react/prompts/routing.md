### local SQL service SQL delegate

**Mandatory first step for database questions.** For every question that needs rows, counts,
aggregates, rankings, lists or comparisons computed from the configured SQL database, the parent
must call `task(subagent_type="nl2sql-react", description=...)` as its first tool call. Do this
before you inspect files, list directories, open a database with `sqlite3` or Python, or write
SQL yourself. This applies
even when the question looks short, uses only one table, or names columns that you could guess.
A database file in the workspace, the input files or the task text does not change the route.
The delegate is bound to the configured database and finds its tables through its own tools, so
do not look for the file first and do not ask the user to name tables or paths. A named database
file is not by itself a different source. Apply the source rule below only when the user says
the question is about a different database than the configured one.

The only database work the parent keeps is mechanical: reading data the user has already pasted
or attached as complete rows, and tasks that have no database question at all (charts, writing,
analysis of results already returned). When in doubt whether something is a database question,
delegate it.

This plugin is the default SQL delegate when it is the only SQL plugin enabled. When other
SQL delegates are enabled, use the user's requested implementation/source and their
declared capabilities; do not query several databases speculatively or substitute a source.
If the route remains ambiguous, ask which source to use.

Pass the original data question and supplied evidence/definitions verbatim. Preserve
time boundaries, units, rounding and decimal precision, ordering, requested columns
and all filters. If you are unsure which parts of a standalone data question affect the
answer, pass the entire question verbatim. Include prior facts only to
resolve references. Keep separate charts and analysis with the parent. Do not guess SQL
or schema for the delegate. Mere absence of schema information is a reason to delegate, not
to ask the user to name tables.

The description must contain only that question and any evidence actually supplied by
the user. Do not add "Notes on intent", assumed entities, likely identifiers, a date
interpretation, units, output requests or other instructions. This subagent owns discovery
and its return contract. For a standalone question Q with no evidence, the task arguments
are exactly `{"subagent_type": "nl2sql-react", "description": Q}`. Compare the payload to
the original before calling task; remove every added sentence.

Keep multiple requested measures or periods within the SAME database question in one
delegation. Do not split them into separate tasks or shorten the question to one measure.
The delegate can return several columns or rows from one checked query.

When Local returns `status: success`, treat its executed result as the answer to the
delegated question. If it covers the user's explicit requirements, present the result
and its artifacts immediately. When returning SQL, use Local's SQL verbatim.
Do not run a second query, fetch supporting rows, or recompute the result just to
verify a successful answer.

After success, delegate the same question again only when you can identify a specific
requirement in the original request that the result missed, such as a requested column,
filter, or decimal precision. Preserve the original question and evidence in the next
description, and state exactly which requirement needs to be met. Continue with any
independent task the user explicitly requested.

Run Local tasks sequentially and inspect each result. A turn may start at most three
Local tasks, including the first and any failures. Repeating an identical description
reuses its result. Preserve any explicit partial-result limitation.

If the turn has multiple successful Local results and needs one final SQL, call
`nl2sql-react__select_result` with the query_id of the result that meets the request.
One success is selected automatically; a failed retry does not discard a success.
Use earlier drafts or rejected review results to inform retries only. They are not
executed answers. When a review asks for clarification, retry only if the original
request or existing evidence resolves the question. Otherwise, ask the user.

For a routine query, deliver the requested answer concisely, with only the definitions,
assumptions and limitations needed to interpret it, plus the saved artifact references.
For a long result, show a small preview and link the complete data; state clearly that
the preview is partial. Preserve the delegate's rows, units and precision. Do not merge
rows into a newly invented grouping or repeat SQL and execution details unless requested.
If the user explicitly asks for the full list, SQL or an explanation, provide it.
Use the SQL returned by Local verbatim when SQL is requested. Use Local's saved
results for follow-up charts or explanations. Never reconstruct or improve its SQL.

The local SQL service database tools are available only to the NL2SQL subagent.
Send its database questions through task; the parent presents the returned result. Do not
use `execute`, shell database clients, Python database drivers, another gateway or local
configuration files to read or query the database instead of delegating. That includes a
quick `.tables`, `.schema` or `SELECT` to "look first". Return the delegate's error, missing choice
or partial-result limitation honestly; do not turn a failed query or truncated result into a
verified complete answer.
If the delegate stops and you cannot supply a concrete resolution, return that reason
to the user. Do not restart the unchanged question, switch delegates or run parent SQL
to bypass the stop. A one-time best-effort policy is an external plugin configuration,
not an instruction the parent may invent or enable during a task.
