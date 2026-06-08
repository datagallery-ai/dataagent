---
name: ontology_service
description: "Use when a task needs ontology or knowledge-graph HTTP data: schema discovery, node or edge lookup, property filtering, multi-hop traversal, pattern search, counts, aggregation, sorted retrieval, or server-declared ontology action execution through configured ONTOLOGY_URL endpoints."
---

# Ontology Service

Use this skill to query a Galatea ontology / knowledge-graph service through deterministic Python CLIs instead of guessing graph labels, properties, IDs, or action parameters.

## Public Interface

Use one entrypoint for all normal work:

```bash
python scripts/ontology_cli.py <command> ...
```

Resolve relative paths against `SKILL_WORKSPACE_PATH` returned by `load_skill`. Prefer `ontology_cli.py`; inspect other files under `scripts/` only when maintaining the skill.

## Connection

Configure the service with command flags or environment variables:

- `ONTOLOGY_URL`: base URL, or a JSON / Python dict string keyed by scene name.
- `SCENE`: optional scene name passed as `scene_name=...`.
- `ONTOLOGY_SEARCH_URL`: optional explicit search endpoint.
- `ONTOLOGY_ACTION_URL`: optional explicit action endpoint.

If only `ONTOLOGY_URL` is set, search and action endpoints are derived automatically.

## Workflow

1. If unsure which operation fits, run `catalog`.
2. Run `describe` when object labels, relation labels, or attributes are unknown.
3. Use `property-filter`, `object-info`, or `relation-info` to find candidate nodes or edges.
4. Use `node-info`, `edge-info`, or `property-info` for full details after you have a concrete UUID.
5. Use `hop` only after a node UUID is known; use `pattern` only after start type, relation type, direction, and end type are known.
6. Use `count-search`, `aggregate-search`, or `sorted-search` for counting, numeric aggregation, ranking, or ordered retrieval.
7. For action execution, run `list-actions` first, match the returned action definition to the user intent, then call `run-action` with discovered IDs and parameters.

Do not invent labels, relation names, property names, IDs, UUIDs, or action parameters. Get them from `describe`, `list-actions`, or earlier query results.

## Data Validation

For multi-table analysis, validate before joining:

- Inspect each table with `property-filter --filter-dict '{}' --get-all-properties --limit 3`.
- Count rows with `count-search` before full retrieval or pagination.
- Check join-key overlap before merging related tables.
- Confirm post-join row counts to detect dropped or duplicated records.
- Check label and behavior-field coverage before drawing segment conclusions.

If ID overlap is below 70% or labeled-user behavior coverage is below 50%, stop and investigate or clearly qualify the conclusion.

## Large Query Rules

- Always set `--limit` for large or unfamiliar tables.
- Prefer `--limit 10000` plus pagination over very large single requests.
- Increase `--timeout` for deep pagination or large result windows.
- Use `--return-properties` to request only fields needed for the task.

## References

- `references/public-api.md`: command contract and input rules.
- `references/commands.md`: concrete command examples, pagination guidance, and data-quality checks.
