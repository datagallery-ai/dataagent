# Deployment from packages

Use this guide when you already have the release artifacts. Install the wheel on the Server machine and the TUI package on the client machine. You can use the same machine for both.

| Artifact | Purpose | Requirements |
| --- | --- | --- |
| `datagallery_dataagent-0.2.0-py3-none-any.whl` | Install the Python Server, CLI, SDK and built-in plugins | Python 3.11 or 3.12, with uv or pip |
| `datagallery-tui-0.2.0.tgz` | Install the terminal client for a running Server | Node.js ≥22.12.0 and npm |

Replace the example filenames with the delivered filenames. Web uses a frontend built from source. Installation resolves each package's dependencies from an available index or prepared offline artifacts. See [Release](../../development/release.md) for build commands and [connect source frontends](deploy-source.md#connect-source-frontends) for Web.

## 1. Install and start the Server

Create an environment and install the wheel from the directory containing it:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python ./datagallery_dataagent-0.2.0-py3-none-any.whl
```

With pip, create the environment using `python3.11 -m venv .venv`, then run `.venv/bin/python -m pip install ./datagallery_dataagent-0.2.0-py3-none-any.whl`.

The installed entry point is `dataagent serve`. For the default template, set the following in the Server terminal or the Home `.env` file:

```bash
export LLM_MODEL=your-model-name
export LLM_BASE_URL=https://your-openai-compatible-host/v1
export LLM_API_KEY=replace-with-your-key
.venv/bin/dataagent serve
```

On Windows PowerShell, set variables with `$env:NAME="value"`. Use `.\.venv\Scripts\python.exe` for installation and `.\.venv\Scripts\dataagent.exe` for startup.

The Server runs in the foreground. Press Ctrl+C to stop it. On its first start, it creates missing templates in `~/.dataagent`. To use a different Home, set `DATAAGENT_HOME` before startup. By default, the Server reads the Home `config.yaml` and `.env`. Use `--config` to select a different complete YAML file and `--env-file` to select a different environment file. To read an environment file in the deployment directory, pass it explicitly with `--env-file`.

The default bind address is `127.0.0.1:8790`. Change `server.host` and `server.port` in YAML and restart to rebind. For a remote client, set an accessible bind address on the Server and use its reachable address from the client. Check the [security boundaries](../concepts/security.md) before exposing the Server beyond loopback. For input directories and CLI options, see the [configuration quick reference](../reference/configuration-api.md) and [CLI reference](../reference/cli-and-environment.md).

Check from another terminal:

```bash
curl -fsS http://127.0.0.1:8790/healthz
curl -fsS http://127.0.0.1:8790/ready
```

HTTP 200 from `/healthz` confirms the process is alive. HTTP 200 from `/ready` confirms the conversation engine loaded its configuration. An unconfigured model results in HTTP 503 from `/ready`. Send a question from the TUI to verify a streamed model response. Server logs are in its Home under `runtime/logs/backend.log`.

<a id="connect-tui"></a>

## 2. Install and start the TUI

From the directory containing the tarball, install it and run the npm entry point:

```bash
npm install -g ./datagallery-tui-0.2.0.tgz
dataagent-cli --runtime-url http://127.0.0.1:8790
```

`da` is an alias for the same entry point. For remote deployment, replace the loopback URL with a Server address reachable from the client. Use `--resume` or `--resume <threadId>` to restore an existing conversation. The Server continues running when the TUI exits. Client diagnostics are in the client Home under `runtime/logs/tui.log`.

<a id="compiled-entry"></a>

## 3. Select a compiled plugin entry (optional)

The default startup uses the default Agent. To run a compiled subagent as the entry for conversations, set `dataagent.entry` to the subagent's `name` from an enabled plugin. For example, the built-in nl2sql subagent is named `nl2sql`. Its declaration already defines `entrypoint: subagents/nl2sql.py:build_subagent` inside the wheel. Select it with `entry: nl2sql`.

Save the following as `nl2sql.yaml` for a SQLite deployment. Retain the model settings from above. Set `NL2SQL_DATABASE_PATH` to the absolute path of an existing SQLite file and `SEMANTIC_BASE_URL` to a reachable semantic service.

```yaml
schema_version: 4
dataagent:
  model: primary
  entry: nl2sql
models:
  primary:
    provider: openai
    name: $env{LLM_MODEL}
    base_url: $env{LLM_BASE_URL}
    api_key: $env{LLM_API_KEY}
plugins:
  enabled: [nl2sql]
  config:
    nl2sql:
      model: primary
      database:
        db_id: mydb
        dialect: sqlite
        engine: sqlite
        path: $env{NL2SQL_DATABASE_PATH}
semantic:
  base_url: $env{SEMANTIC_BASE_URL}
```

```bash
.venv/bin/dataagent serve --config ./nl2sql.yaml
```

Connect with the same TUI command. After changing `dataagent.entry`, restart the Server and begin a new conversation so it gets the selected graph's state. Once the health checks pass, send a real request that exercises the entry.

To adapt your own plugin, place it under `plugins/` in the Server Home. In its subagent declaration, set `kind: compiled` and point `entrypoint` to `file.py:symbol` relative to the plugin directory. That symbol implements `factory(params, services)` and returns a Runnable. Add the plugin ID to `plugins.enabled` and the subagent's `name` to `dataagent.entry`. [Compiled subagent and entry contracts](../guides/integrate/extensions/subagents.md#entry-mode)

## Optional dependencies { #optional-dependencies }

SQLite, MySQL and PostgreSQL drivers are included as wheel dependencies. Append `[video]`, `[spark]` or `[all]` to the wheel filename as needed:

```bash
uv pip install --python .venv/bin/python "./datagallery_dataagent-0.2.0-py3-none-any.whl[video]"
```

`video` installs video processing dependencies, `spark` installs PySpark, and `all` selects both. Spark/Hive also requires Java 17 or later and the corresponding service configuration. Configure plugins and database connections in the Server YAML.
