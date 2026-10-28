# dataops_probe_table — Tool Schema

> Source: `dataagent/actions/tools/local_tool/dataops_probe_tool.py`
> This is a **reference excerpt** for LLM tool-spec. Read SKILL.md first for the
> "when to use / how to pick partition_filter" decision logic.

## Signature (ToolWrapper-facing)

```
dataops_probe_table(
    table: str,
    columns: list[str] | None = None,
    *,
    partition_filter: str | None = None,
    sample_size: int = 5,           # clamped to [1, 50]
    timeout_sec: int = 60,          # clamped to [1, 300]
) -> dict
```

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
  "sample_size": 5,
  "elapsed_sec": 3.2,
  "data_source": "prod" | "integration"
}
```

Skipped (validation fail):
```json
{
  "ok": false,
  "skipped": true,
  "table": "db.tbl",
  "reason": "empty_table" | "invalid_table_name" | "invalid_partition_filter",
  "error": "human readable",
  "elapsed_sec": 0.0
}
```

Failed (backend fail):
```json
{
  "ok": false,
  "skipped": false,
  "table": "db.tbl",
  "reason": "status_failed" | "poll_timeout" | "unknown_error" | "submit_failed",
  "errorLog": "backend error log excerpt (head+tail, bounded — not the full log)",
  "elapsed_sec": 60.1
}
```

## Whitelist reason codes (skipped=True)

| reason | cause | fix |
|---|---|---|
| `empty_table` | table is empty/whitespace | pass non-empty `db.tbl` |
| `invalid_table_name` | identifier has non-`[A-Za-z0-9_]` chars or starts with digit | rename or remove bad chars |
| `invalid_partition_filter` | string doesn't match regex, contains `$date`/OR/`;`, or date segment is semantically invalid | rewrite per SKILL.md §"partition_filter 格式" |
