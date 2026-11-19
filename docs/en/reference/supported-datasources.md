# Supported inputs

This page only lists the kinds of input you can give the model. The steps for attaching a directory are in [Where data comes from](../guides/data-sources.md).

The control plane does not serve `GET /api/v1/datasource-types`, and it has no registry of database types.

What you can give the model today:

| Input | How to configure it |
| --- | --- |
| A local directory | `dataagent.workspaces`, or `--workspace` / `DATAAGENT_WORKSPACE` |
| A Skill | `<home>/skills/<name>/SKILL.md` |
| A plugin | `<home>/plugins/<id>/` or `plugins.paths`, then `plugins.enabled` |
| MCP | `.mcp.yaml` at Home or at an enabled plugin root |
| Semantic service | The `semantic` object in `config.yaml`. Omit it and no client is created. |

Querying a database, warehouse, or spreadsheet requires that an enabled plugin declare it in its own configuration. Do not call REST from the old datasource type table.
