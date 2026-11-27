# Web workbench

Use the browser to ask questions, reopen a session, and view outputs from the same Server that the terminal client uses.

## Start

Prepare the source checkout using the [source deployment guide](deploy-source.md). With dependencies and model settings in place, run from the repository root:

```bash
npm run build:web
npm run start
```

`npm run start` starts Web and uses the local Server at the address selected by the launcher. It starts a Server when that address has no running control plane. Open [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks) or use the Web address printed by the launcher. To change the Web port or bind address, pass `--web-port` or `--web-host` after `--`.

<a name="connect-server"></a>

### Connect Web to an existing Server

After building Web, use Web-only startup. If the Server is already listening at the address in Home `config.yaml`, run `npm run start:web`. For a Server at another address, set the Server URL on the machine running Web:

```bash
API_PROXY_TARGET=http://127.0.0.1:8791 npm run start:web
```

On PowerShell, set `$env:API_PROXY_TARGET = 'http://127.0.0.1:8791'` before running `npm run start:web`. The launcher uses the explicit `API_PROXY_TARGET` in Web-only mode when `--config` is omitted. If you pass `--config`, it reads the Server address from that YAML. Without either override it reads Home `config.yaml`, with a default of `http://127.0.0.1:8790`.

The browser calls same-origin `/api/v1/*` routes. The Next.js Server proxies those requests to the configured Server and streams the reply to the browser. See [source frontend connections](deploy-source.md#connect-source-frontends).

## Ask

1. Open `/data-tasks` and enter a question.
2. To continue a previous conversation, select it from the session list. To stop the active turn, use the stop control in chat.
3. Open the task console to view the current session's outputs. You can also open the same session and its files in the [TUI](tui.md).

The Server supplies the model, plugins, and the session's input directories. To change supported settings, follow the [configuration guide](../reference/configuration-api.md). Session routes, file previews, and cancellation responses are in the [REST reference](../reference/rest-api.md#sessions-and-runs).

<a name="troubleshooting"></a>

## Troubleshooting

- If the page is unavailable, check that the Web process is running and open the address printed by the launcher.
- If a proxied request returns `502`, check that Web can reach the configured Server address. Call `/healthz` and `/ready` on the Server address itself. A `503` from `/ready` calls for checking model configuration and backend logs.
- Older Web panels may still send requests for unimplemented resources and receive `404`. Check the [capability matrix](../concepts/capabilities.md) for supported operations.
