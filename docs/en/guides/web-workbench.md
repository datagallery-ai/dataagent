# Web workbench

The Web app is one presentation of the control plane. It does not keep its own model, workspace, or session store.

## Start

Control plane and Web together:

```bash
npm run start
```

Web only, proxying to a control plane that is already listening:

```bash
npm run start:web
```

Open:

```text
http://127.0.0.1:3000/data-tasks
```

Do not start at `/login`. The control plane has no registration or login routes. `GET /api/v1/me` returns the process `--user`.

The browser calls same-origin `/dataagent/stream` and `/api/v1/*`. Next forwards them using `server.host` and `server.port` from `config.json`, default `http://127.0.0.1:8790`.

Readiness:

```bash
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
```

## Ask

Type a question on `/data-tasks`. The turn goes to `POST /dataagent/stream`. The control plane keeps history by `threadId`. The model, plugins, and workspaces are whatever the process loaded at startup. The page cannot change them.

The session list comes from `GET /api/v1/sessions`. Cancel an active turn with `POST /api/v1/runs/{runId}/cancel`.

If the page still shows forms for datasources, knowledge bases, model profiles, or file assets, those requests hit missing routes and return 404. Edit Home `config.json` and restart the control plane. See [Configuration](../reference/configuration-api.md).

## Relation to the TUI

The TUI uses the same port. Sessions and outputs are the same files. See [TUI guide](tui.md).
