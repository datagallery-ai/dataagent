---
name: feature_engineering
description: |
  Use when: The user requests feature production for warehouse/SQL pipelines and expects production-ready feature SQL deliverables and a SQL-centric report.
  NOT for: Pure dashboard/data-analysis-only tasks, pure model training/tuning, environments that cannot execute SQL or validate schema.
---

# Feature Engineering (SQL Production First)

## When to Run

- User provides table/database sources and asks for production-ready features.
- The final deliverable must be SQL-first, not notebook-style analysis.
- Output expectation: production-ready feature SQL deliverables as defined by the scenario/config.

## Core Constraints (Must Follow)

1. **Scenario-first output contract**: final output artifacts must follow the scenario/config prompt requirements (for example DDL/INSERT/report format).
2. **No generic analysis drift**: every step must contribute directly to feature SQL design, validation, or productionization.
3. **Phase ordering**: source understanding and data quality checks must complete before final SQL freeze.
4. **Constraint absorption**: absorb user business goal, feature granularity, windows, and allow/block constraints before SQL drafting.
5. **Validation required**: validate row count, null rate, uniqueness, and leakage risk before SQL freeze.
6. **Incremental SQL evolution**: show how draft SQL changes based on evidence.
7. **Report scope**: report is SQL-centric and should include field/business/source mapping when required by scenario.
8. **No model training**: do not train predictive models in this skill.
9. **SQL standard dependency**: when drafting/reviewing production SQL, explicitly consult the `sql_writing_standard` skill and follow its hard rules for grain, safe joins, ratio guards, and idempotent write strategy.

## Input Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `<INPUT_SOURCE>` | Yes | Upstream tables/database path and metadata |
| `<OUTPUT_ROOT>` | Yes | Output directory for SQL/report artifacts |
| `<BUSINESS_GOAL>` | Yes | Business target for feature construction |
| `<FEATURE_GRAIN>` | No | Feature table grain (e.g., user-day, request_id) |
| `<WINDOWS>` | No | Time windows (e.g., 1/7/30 day) |
| `<USER_CONSTRAINTS>` | No | Feature allow/block lists, business rules, SLA constraints |

## Workflow

Execute the following six phases in order. Before each phase, announce goal and success criteria.

## Phase Output Contract (Applies to Every Phase)

Before execution:
- Phase goal (1–2 lines)
- Sub-steps (goal -> SQL/tool -> success criteria)
- Inputs & assumptions (source tables, keys, time fields, grain)
- Expected artifacts (SQL snippets, checks, files)
- Risks & fallback (1–2 lines)

Then execute. After execution, always output:
- What I found (SQL evidence / row counts / schema checks)
- Decisions & next step (1–3 lines)

---

### Phase 1: Source & Grain Definition

**Goal**: lock input tables, primary keys, time field, and target feature grain.

1. Inspect source schema and identify candidate entity keys.
2. Confirm feature grain (`<FEATURE_GRAIN>`) and snapshot/event time column.
3. Record join paths between source tables.
4. Produce a source mapping table in report.

**End of Phase 1**: source tables, keys, grain, and join paths are locked.

### Phase 2: SQL-oriented Data Quality Checks

**Goal**: run mandatory quality SQL checks before feature generation.

1. Null-rate checks on key fields and feature candidates.
2. Duplicate checks at target grain.
3. Join cardinality checks (1:1, 1:N, N:1).
4. Time leakage checks (feature timestamp must not exceed label/event timestamp).
5. If severe issues exist, propose revised SQL route and wait for confirmation.

**End of Phase 2**: quality issues documented; remediation plan confirmed.

### Phase 3: Feature Blueprint & SQL Skeleton

**Goal**: define feature groups and draft SQL skeleton.

1. Define feature groups (user intent, item attractiveness, shop quality, context exposure, interactions).
2. Map each group to SQL expressions and source tables.
3. Define windows (`<WINDOWS>`) and aggregation logic.
4. Build SQL CTE skeleton with clear module boundaries.
5. Absorb `<USER_CONSTRAINTS>` explicitly.

**End of Phase 3**: feature groups, SQL expressions, and CTE skeleton are drafted.

### Phase 4: Feature SQL Construction

**Goal**: iteratively implement final feature SQL logic.

Each sub-task follows: **State intent -> Draft SQL -> Execute SQL -> Observe -> Revise**.

1. Implement base entities CTE.
2. Implement window aggregations (1/7/30d etc.).
3. Implement cross-entity interaction features.
4. Apply null/default strategies and stable type casting.
5. Merge CTEs into final SELECT aligned to target grain.

**End of Phase 4**: draft feature SQL is executable and aligned to target grain.

### Phase 5: SQL Freeze & Validation

**Goal**: freeze final SQL package and verify it is production-safe.

Mandatory checks:
1. Row-count sanity at target grain.
2. Uniqueness check on business key.
3. Null-rate checks for mandatory columns.
4. Distribution spot-checks on core metrics.
5. Leakage/temporal consistency checks.
6. Basic performance check (filter pushdown / partition pruning / avoid Cartesian join).

**End of Phase 5**: SQL is frozen; all validation checks pass.

### Phase 6: Deliver Artifacts

**Goal**: produce all deliverables defined by the scenario/config and write them to `<OUTPUT_ROOT>`.

1. Generate every artifact required by the scenario contract (e.g., DDL, INSERT, report, design doc).
2. Verify artifact consistency (field alignment, executable SQL, correct paths).
3. Output a brief delivery summary listing each artifact and its path.
