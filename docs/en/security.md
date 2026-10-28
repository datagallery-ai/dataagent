# Security

This guide is for trial users, integration developers, and maintainers preparing public demos. After reading it, you will know how credentials appear in public docs, data source connection boundaries, and local development security limits.

## Credential examples in docs

Public docs and examples must use placeholder values only:

```text
replace-with-your-key
your-api-key
```

Do not put real model keys, database passwords, MCP tokens, private keys, cookies, personal access tokens, or internal network addresses in README, docs, issue examples, or screenshots.

## Agent run boundaries

When starting a run, clients send resource IDs and selection only:

- `activeDatasourceId`
- `enabledDatasourceIds`
- `enabledKnowledgeIds`
- `enabledMcpServerIds`
- `enabledSkillIds`
- `fileIds`

Do not put database passwords, model API keys, MCP tokens, or full connection strings in AG-UI `messages`, `context`, `state`, or `forwardedProps`.

## Resource configuration boundaries

Credentials for data sources, models, MCP servers, and Skills are submitted only when creating or updating resources. Read APIs return `secretRef`, `hasSecret`, or equivalent markers—not plaintext credentials.

When creating resources through REST API, put credentials in resource configuration fields—not in natural-language questions:

```json
{
  "id": "sales-pg",
  "name": "Sales PostgreSQL",
  "type": "postgresql",
  "config": {
    "host": "127.0.0.1",
    "port": 5432,
    "database": "sales",
    "username": "readonly"
  },
  "credentials": {
    "password": "replace-with-your-key"
  }
}
```

## Data source connection recommendations

- Use read-only accounts or test databases for first integration.
- Grant minimum permissions for PostgreSQL, MySQL, SQL Server, Oracle, Snowflake, BigQuery, and other external services.
- Set reasonable `maxRows` and `timeoutMs` for queries.
- Configure `maskFields` for email, phone, ID numbers, and similar fields.
- Use allowlists for sensitive databases and tables.
- SQLite, CSV, Excel, and DuckDB file paths must be accessible to the backend process.

## Identity and sessions

The workbench does not read accounts, mail, or database paths from environment variables. Optional server model settings are `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY`. The control plane address is `server.host` and `server.port` in Home `config.json`. The web address uses `--web-host` and `--web-port`, default `127.0.0.1:3000`.

The browser uses the same-origin proxy. Start with `npm run build && npm run start`. Split processes use `npm run start:api` and `npm run start:web`.

Real production also needs secret management, audit export, access control, and operations monitoring.

## Documentation release checks

Before publishing public docs, run at least:

```bash
npm run smoke:docs
```

Maintainers should also scan locally for source-sensitive terms, personal paths, real credentials, and release-blocked wording. If a scan hits real sensitive content, remove it or replace with example values. Do not explain the origin of sensitive content in public docs.
