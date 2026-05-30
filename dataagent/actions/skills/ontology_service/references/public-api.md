# Ontology Service Public API

Use this skill through one stable entrypoint:

```bash
python .skills/ontology_service/scripts/ontology_cli.py <command> ...
```

## Discovery

When uncertain, inspect the public contract first:

```bash
python .skills/ontology_service/scripts/ontology_cli.py catalog
python .skills/ontology_service/scripts/ontology_cli.py --help
python .skills/ontology_service/scripts/ontology_cli.py <command> --help
```

## Command Contract

- `catalog`
  Prints the command manifest in JSON. Supports `--catalog-command <name>` to focus on one command.
- `describe`
  Returns ontology schema information.
- `object-info --object-type <class> [--limit <n>] [--offset <n>]`
  Lists node instances of one object type.
- `node-info --object-type <class> --uuid <uuid>`
  Returns one node's details.
- `relation-info --relation-type <type> [--limit <n>] [--offset <n>]`
  Lists edge instances of one relation type.
- `edge-info --relation-type <type> --uuid <uuid>`
  Returns one edge's details.
- `relation-by-start --start-node-uuid <uuid> --relation-type <type>`
  Finds one-hop matching relations from a start node.
- `relation-by-end --end-node-uuid <uuid> --relation-type <type>`
  Finds one-hop matching relations from an end node.
- `property-filter --element-class <class> --element-type NODE|EDGE --filter-dict '<json>' [--get-all-properties] [--limit <n>] [--offset <n>]`
  Filters graph elements by property conditions.
- `property-info --element-class <class> --element-uuid <uuid>`
  Returns property descriptions and values.
- `count-search --element-class <class> --element-type NODE|EDGE --filter-dict '<json>'`
  Counts matching rows.
- `aggregate-search --element-class <class> --element-type NODE|EDGE --target-property <prop> --agg SUM|AVG|MIN|MAX|COUNT --filter-dict '<json>'`
  Runs one aggregate query.
- `sorted-search --element-class <class> --element-type NODE|EDGE --filter-dict '<json>' --sort-by <prop> [--return-properties '<json-list>'] [--descending] [--limit <n>] [--offset <n>]`
  Runs one sorted query. **Note**: `--sort-by`, `--element-class`, `--element-type` are all **required**.
- `hop --uuid <uuid> --hop-num <n> [--accurate]`
  Runs multi-hop traversal.
- `sub-graph --uuid <uuid> [--limit <n>]`
  Gets a two-hop neighbourhood subgraph centered on one node.
- `pattern --start-object-type <type> --relation-type <type> --direction -|out|in --end-object-type <type> [--limit <n>] [--offset <n>]`
  Runs start-relation-end pattern search.
- `list-actions`
  Lists server-declared ontology action definitions for the current scene.
- `run-action (--action-name <name> | --action-id <id>) [--instance-type <type>] [--instance-api-name <name>] [--instance-id <id>] [--input-params '<json>']`
  Runs one server-declared ontology action. Use `list-actions` first.

## Input Rules

- Pass structured values as JSON strings when possible.
- `--filter-dict` accepts JSON or Python-literal dict strings.
- Plain filter values are auto-wrapped: `{"name": "Alice"}` becomes `{"name": "= 'Alice'"}`. No need to write operators for equality.
- `--return-properties` accepts JSON or Python-literal list strings.
- Always use `--limit` to cap result rows for large or unfamiliar tables, for example `--limit 10000`.
- Increase `--timeout` for large queries, for example `--timeout 180` when using large limits or deep pagination.
- All commands print JSON to stdout.
