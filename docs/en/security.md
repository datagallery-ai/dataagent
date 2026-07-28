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

DataFoundry only supports cookie-based password sessions. Dev tokens, `/api/v1/dev/*`, and `DATAFOUNDRY_AUTH_MODE` are removed.

Required settings:

```text
AUTH_SESSION_SECRET=replace-with-at-least-32-random-characters
AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000
AUTH_REGISTRATION_MODE=open
AUTH_EMAIL_DELIVERY=test
AUTH_EMAIL_FROM=DataFoundry <no-reply@example.com>
```

`AUTH_REGISTRATION_MODE` is required (`open` = self-register, `closed` = reject register with `REGISTRATION_CLOSED`). Deploy defaults to `open` for formal local/test; set `closed` for internet-facing installs that should not accept public signup. `GET /api/v1/me` returns the current user; `GET /api/v1/auth/status` exposes `registrationEnabled` without secrets.

Two formal environments share the same start commands:

| Environment | `AUTH_EMAIL_DELIVERY` | `AUTH_PUBLIC_BASE_URL` | Typical `AUTH_REGISTRATION_MODE` |
| --- | --- | --- | --- |
| Formal test | `test` (links in API console) | Loopback HTTP URL | `open` |
| Real production | `smtp` (plus `AUTH_SMTP_*`) | Public HTTPS origin | `closed` unless self-signup is intentional |

`/api/v1/auth/*` covers registration, login, email verification, password reset, logout, session listing, and password change. Unsafe requests require `X-CSRF-Token` from the `df_csrf` cookie. The session cookie is `df_session`. Cookie `Path` / `Secure` follow `AUTH_PUBLIC_BASE_URL` (HTTPS ⇒ `Secure`; pathname prefix becomes cookie path). `AUTH_EMAIL_DELIVERY=test` is rejected unless `AUTH_PUBLIC_BASE_URL` is loopback.

Leave `NEXT_PUBLIC_AGENT_RUNTIME_URL` / `NEXT_PUBLIC_CONFIG_API_URL` empty so the browser uses the same-origin Next BFF; point the upstream API with `API_PROXY_TARGET` in `apps/web/.env.local`. Start with `npm run build && npm run build:web && npm run start:api && npm run start:web`. Real-production reverse-proxy sample: `deploy/nginx.datafoundry.conf.example`.

If an old `.env` still has `DATAFOUNDRY_AUTH_MODE=password`, the API ignores that value; `./deploy.sh deploy` strips it from written config. `DATAFOUNDRY_AUTH_MODE=dev` fails startup.

Password-only does **not** migrate Metadata databases that still have `users.dev_token`. Opening such a DB fails with `METADATA_SCHEMA_INCOMPATIBLE`. Stop the stack, reset `STORAGE_ROOT_DIR` / `METADATA_DB_PATH` / `MASTRA_STORAGE_PATH` / `FILE_ASSET_STORAGE_ROOT` / `WORKSPACE_ROOT` (or point them at empty paths), restart, and register again. There is no in-place schema upgrade for this cutover.

Real production also needs secret management, audit export, access control, and operations monitoring.

## Documentation release checks

Before publishing public docs, run at least:

```bash
npm run smoke:docs
```

Maintainers should also scan locally for source-sensitive terms, personal paths, real credentials, and release-blocked wording. If a scan hits real sensitive content, remove it or replace with example values. Do not explain the origin of sensitive content in public docs.
