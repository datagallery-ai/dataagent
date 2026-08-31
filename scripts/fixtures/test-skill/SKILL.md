---
name: hello-test-skill
description: A minimal skill for upload and runtime smoke testing.
version: 1.0.0
tags:
  - test
  - demo
user-invocable: true
allowed-tools:
  - inspect_schema
  - run_sql_readonly
  - preview_table
---

# Hello Test Skill

Use this skill when the user asks for a simple upload test or a basic data check.

## Steps

1. Greet the user and confirm this skill was loaded.
2. If a datasource is available, call `inspect_schema` first.
3. Run a small read-only query with `run_sql_readonly` or preview a table.
4. Summarize results in plain language.

## Notes

- This is a test skill only. Keep responses short.
- Do not invent schema or query results.
