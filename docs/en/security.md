# Security

This guide is for trial users, integration developers, and maintainers preparing public demos. After reading it, you will know how credentials appear in public docs, data source connection boundaries, and local development security limits.

## Credential examples in docs

Public docs and examples must use placeholder values only:

```text
replace-with-your-key
your-api-key
<dev_token>
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

## Local development boundaries

Local development APIs accept dev tokens and the default workspace:

```text
Authorization: Bearer <dev_token>
X-Dev-Token: <dev_token>
X-Workspace-Id: default
```

When headers are omitted, the backend uses the development default identity and default workspace. Web v1 treats one user as owning `default`; it does not expose workspace switching. If you build a client, send the same identity headers to REST `/api/v1/*` calls and CopilotKit `/api/copilotkit` runs so configuration, sessions, files, artifacts, and run history stay in the same user scope.

Development identity endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/me` | Read the current user and workspace. |
| GET | `/api/v1/dev/identities` | List local development users. |
| POST | `/api/v1/dev/users` | Create or update a local development user. |

`/api/v1/dev/*` is disabled in production unless `DATAFOUNDRY_ENABLE_DEV_IDENTITY_API=true`.

## Password authentication mode

Set `DATAFOUNDRY_AUTH_MODE=password` for cookie-based password authentication. Formal mode uses `password` by default. Required settings:

```text
AUTH_SESSION_SECRET=replace-with-at-least-32-random-characters
AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000
AUTH_EMAIL_DELIVERY=test
AUTH_EMAIL_FROM=DataFoundry <no-reply@example.com>
```

Two formal environments share the same start commands:

| Environment | `AUTH_EMAIL_DELIVERY` | `AUTH_PUBLIC_BASE_URL` |
| --- | --- | --- |
| Formal test | `test` (links in API console) | Local or private URL |
| Real production | `smtp` (plus `AUTH_SMTP_*`) | Public HTTPS origin |

Password mode adds `/api/v1/auth/*` endpoints for registration, login, email verification, password reset, logout, session listing, and password change. Unsafe requests require `X-CSRF-Token` from the `df_csrf` cookie. The session cookie is `df_session`.

Also set `NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=password` and leave `NEXT_PUBLIC_AGENT_RUNTIME_URL` / `NEXT_PUBLIC_CONFIG_API_URL` empty so the browser uses the same-origin Next BFF; point the upstream API with `API_PROXY_TARGET` in `apps/web/.env.local`. Start with `npm run build && npm run build:web && npm run start:api && npm run start:web`. Real-production reverse-proxy sample: `deploy/nginx.datafoundry.conf.example`.

Real production also needs secret management, audit export, access control, and operations monitoring.

## Documentation release checks

Before publishing public docs, run at least:

```bash
npm run smoke:docs
```

Maintainers should also scan locally for source-sensitive terms, personal paths, real credentials, and release-blocked wording. If a scan hits real sensitive content, remove it or replace with example values. Do not explain the origin of sensitive content in public docs.
