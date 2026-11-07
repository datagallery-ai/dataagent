# dataops_probe_table — Tool Schema

> Source: `dataagent/actions/tools/local_tool/dataops_probe_tool.py`
> This is a **reference excerpt** for LLM tool-spec. Read SKILL.md first for the
> "when to use / how to pick partition_filter" decision logic.

## Signature (ToolWrapper-facing)

```
dataops_probe_table(
    table: str,
    columns: list[str],            # REQUIRED — DataOps forbids SELECT *
    *,
    partition_filter: str | None = None,
    sample_size: int = 5,           # clamped to [1, 50]
    timeout_sec: int = 60,          # clamped to [1, 300]
) -> dict
```

`columns` is a **required** parameter in the generated JSON schema
(`required: ["table", "columns"]`). It must be a non-empty list of
single-segment column names — no `*`, no `db.tbl.col` prefixes.

## partition_filter whitelist (regex)

```
^\s*pt_d\s*=\s*'(?P<d>\d{8})'\s*$
```

Only the day partition `pt_d` is accepted. `pt_h` / `pt_i` (hour / minute
partitions) and any other partition column name are rejected.

Any deviation → `{"ok": False, "skipped": True, "reason": "invalid_partition_filter", ...}`.

## Return shape

Success:
```json
{
  "ok": true,
  "table": "db.tbl",
  "rows": [{"col1": "v1", ...}],
  "row_count": 5,
  "job_id": "Q20261009_xxx",
  "sql": "SELECT `col1` FROM `db`.`tbl` WHERE pt_d = '20261009' LIMIT 5",
  "elapsed_sec": 3.2,
  "auto_partition_filter": "pt_d = '20261009'"
}
```

`row_count` is the number of **sampled rows returned** — NOT the table's real
row count. The tool never runs `SELECT count(*)`.

Failed (uniform shape — always `skipped: true`):
```json
{
  "ok": false,
  "skipped": true,
  "table": "db.tbl",
  "reason": "execute_failed" | "poll_error" | "timed_out" | "unknown_error",
  "error": "human readable (bounded)",
  "job_id": "Q20261009_xxx",
  "errorLog": "backend error log excerpt (head+tail, bounded — not the full log)",
  "elapsed_sec": 60.1
}
```

## reason codes

| reason | skipped | cause | fix |
|---|---|---|---|
| `empty_table` | true | table is empty/whitespace | pass non-empty `db.tbl` |
| `invalid_table_name` | true | identifier has non-`[A-Za-z0-9_]` chars or starts with digit | rename or remove bad chars |
| `invalid_columns` | true | `columns` missing/empty, not a list, or an element fails the whitelist (contains `.` or `*`) | fetch schema first, then pass explicit single-segment column names |
| `invalid_partition_filter` | true | string doesn't match regex, contains `$date`/OR/`;`, or date segment is semantically invalid | rewrite per SKILL.md §"partition_filter 规则" |
| `no_dataops_resource` | true | no `dataops` resource in the catalog | mount the resource or continue without probing |
| `dataops_disabled` | true | resource metadata `enabled: false` | set `enabled: true` |
| `dataops_url_empty` | true | resource transport url is empty | set the url |
| `execute_failed` | true | submit / collect failed | network issue — retrying won't help |
| `poll_error` | true | poll raised or returned an error status | network issue |
| `timed_out` | true | submit+poll+collect exceeded `timeout_sec` | raise `timeout_sec` if needed |
| `unknown_error` | true | uncaught exception (catch-all) | check the logs |
