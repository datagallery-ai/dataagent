# Ontology Service Commands

## Connection

Example environment:

```bash
export ONTOLOGY_URL="https://ontology.example.com"
export SCENE="default"
```

Or pass endpoints directly:

```bash
python .skills/ontology_service/scripts/ontology_cli.py describe \
  --search-base-url "https://ontology.example.com/api/v1/search"
```

If unsure which command to use:

```bash
python .skills/ontology_service/scripts/ontology_cli.py catalog
```

## Describe Ontology

```bash
python .skills/ontology_service/scripts/ontology_cli.py describe
```

## Entity Lookup

List all nodes of one type:

```bash
python .skills/ontology_service/scripts/ontology_cli.py object-info \
  --element-class Supplier \
  --element-type NODE
```

Get one node by UUID:

```bash
python .skills/ontology_service/scripts/ontology_cli.py node-info \
  --element-class MPart \
  --element-type NODE \
  --uuid acf6b73e-ef6d-460b-8aa3-189c295db5b1
```

Inspect edges of one relation type:

```bash
python .skills/ontology_service/scripts/ontology_cli.py relation-info \
  --relation-type Fund-INVESTS-Company
```

## Property And Analytics

Filter nodes (plain values auto-wrapped with `=`):

```bash
# All Fund nodes
python .skills/ontology_service/scripts/ontology_cli.py property-filter \
  --element-class Fund \
  --element-type NODE \
  --filter-dict '{}'

# Fund nodes with name containing 'A'
python .skills/ontology_service/scripts/ontology_cli.py property-filter \
  --element-class Fund \
  --element-type NODE \
  --filter-dict '{"name": "CONTAINS '\''A'\''"}'

# Fund nodes named 'Alice' - plain value auto-wrapped
python .skills/ontology_service/scripts/ontology_cli.py property-filter \
  --element-class Fund \
  --element-type NODE \
  --filter-dict '{"name": "Alice"}'
```

Count matches:

```bash
python .skills/ontology_service/scripts/ontology_cli.py count-search \
  --element-class Fund \
  --element-type NODE \
  --filter-dict '{"name": "= '\''Alice'\''"}'
```

Aggregate a numeric property:

```bash
python .skills/ontology_service/scripts/ontology_cli.py aggregate-search \
  --element-class Fund \
  --element-type NODE \
  --target-property AAA \
  --agg AVG \
  --filter-dict '{}'
```

Sorted query (--sort-by, --element-class, --element-type are all required):

```bash
python .skills/ontology_service/scripts/ontology_cli.py sorted-search \
  --element-class Company \
  --element-type NODE \
  --filter-dict '{"industry": "= '\''AI'\''"}' \
  --return-properties '["name", "registered_capital"]' \
  --sort-by registered_capital \
  --descending \
  --limit 100
```

## Graph Search

Multi-hop:

```bash
python .skills/ontology_service/scripts/ontology_cli.py hop \
  --uuid 7da425e2-07b6-4147-adb6-941c60ce5079 \
  --hop-num 2
```

Pattern search:

```bash
python .skills/ontology_service/scripts/ontology_cli.py pattern \
  --start-object-type Fund \
  --relation-type Fund-INVESTS-Company \
  --direction out \
  --end-object-type Company
```

Subgraph (two-hop neighbourhood):

```bash
python .skills/ontology_service/scripts/ontology_cli.py sub-graph \
  --uuid 7da425e2-07b6-4147-adb6-941c60ce5079 \
  --limit 1000
```

## Ontology Actions

List server-declared action definitions for the current scene:

```bash
python .skills/ontology_service/scripts/ontology_cli.py list-actions
```

Run a discovered action by name:

```bash
python .skills/ontology_service/scripts/ontology_cli.py run-action \
  --action-name "<action_name>" \
  --instance-api-name "<entity_type>" \
  --instance-id "<uuid>" \
  --input-params '{}'
```

## Large Queries

Start with a count before fetching broad result sets:

```bash
python .skills/ontology_service/scripts/ontology_cli.py count-search \
  --element-class <element_class> \
  --element-type NODE \
  --filter-dict '{}'
```

Recommended defaults:

| Total rows | Retrieval strategy | Timeout |
| --- | --- | --- |
| < 10,000 | Single query with `--limit 10000` | 60s |
| 10,000 - 100,000 | Paginate with `--limit 10000 --offset ...` | 60-120s |
| 100,000+ | Paginate or split independent filters | 120-180s |

Prefer smaller pages with pagination over very large single requests.

## Data Quality Checks

Inspect schema and sample values before aggregation:

```bash
python .skills/ontology_service/scripts/ontology_cli.py property-filter \
  --element-class <table_name> \
  --element-type NODE \
  --filter-dict '{}' \
  --get-all-properties \
  --limit 3
```

Before joining two result sets, check key overlap:

```python
left_ids = set(row["n.`usid`"] for row in left_rows)
right_ids = set(row["n.`usid`"] for row in right_rows)
matched = left_ids & right_ids
match_rate = len(matched) / max(len(right_ids), 1)
print(f"Matched: {len(matched)} ({match_rate:.1%} of right table)")
```

After joining, verify one output row per primary entity and inspect label coverage before writing behavioral conclusions.
