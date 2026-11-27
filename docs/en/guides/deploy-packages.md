# Deployment from packages

Install the wheel on the Server machine and the TUI package on the client machine. You can use the same machine for both.

<a name="get-artifacts"></a>

## Get the artifacts and check the version

The public [GitCode release](https://gitcode.com/datagallery/dataagent/releases) currently offers the `v0.1.0` source archives. This guide covers `0.2.0`. Follow the steps below to build its Server wheel and TUI package from [DataAgent `main` on GitCode](https://gitcode.com/datagallery/dataagent). The public [PyPI release `0.1.0`](https://pypi.org/project/datagallery-dataagent/0.1.0/) uses the legacy runtime and configuration. For that release, follow the documentation in the [`v0.1.0` source tree](https://gitcode.com/datagallery/dataagent/tree/v0.1.0). See the [legacy migration comparison](../../development/archive/main-legacy-migration.md) when moving to the current runtime.

For a fresh install, build the artifacts from source. On Linux, with Git, Node.js ≥22.12.0, npm, uv, and Make available, run:

```bash
git clone https://gitcode.com/datagallery/dataagent.git
cd dataagent
git rev-parse HEAD
make prepare PYTHON_VERSION=3.12
make build PYTHON_VERSION=3.12
sha256sum dist/server/*.whl dist/tui/*.tgz
```

To reproduce a specific build, check out the recorded source commit before running the build commands. Record the Git commit, artifact filenames, and SHA-256 values together. For artifacts supplied by someone else, check the build commit and compare the files against the supplier's published SHA-256 values before installing. See [build and artifact verification](../../development/release.md#verify-artifacts).

| Artifact | Purpose | Requirements |
| --- | --- | --- |
| `datagallery_dataagent-0.2.0-py3-none-any.whl` | Install the Python Server, CLI, SDK and built-in plugins | Python 3.11 or 3.12, with uv or pip |
| `datagallery-tui-0.2.0.tgz` | Install the terminal client for a running Server | Node.js ≥22.12.0 and npm |

Replace the example filenames with the delivered filenames. Web uses a frontend built from source. Installation resolves each package's dependencies from an available index or prepared offline artifacts. See [Release](../../development/release.md) for build commands and [connect Web to an existing Server](web.md#connect-server).

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

The default bind address is `127.0.0.1:8790`. Change `server.host` and `server.port` in YAML and restart to rebind. For a remote client, set an accessible bind address on the Server and use its reachable address from the client. Check the [security boundaries](../concepts/security.md) before exposing the Server beyond loopback. For input directories and CLI options, see the [configuration quick reference](configuration.md) and [CLI reference](../reference/cli-and-environment.md).

Check from another terminal:

```bash
curl -fsS http://127.0.0.1:8790/healthz
curl -fsS http://127.0.0.1:8790/ready
```

HTTP 200 from `/healthz` confirms the process is alive. HTTP 200 from `/ready` confirms the conversation engine loaded its configuration. An unconfigured model results in HTTP 503 from `/ready`. Send a question from the TUI to verify a streamed model response. Server logs are in its Home under `runtime/logs/backend.log`.

<a name="2-install-and-start-the-tui"></a>

<a name="connect-tui"></a>

## 2. Install and start the TUI

From the directory containing the tarball, install it and run the npm entry point:

```bash
npm install -g ./datagallery-tui-0.2.0.tgz
dataagent-cli --runtime-url http://127.0.0.1:8790
```

`da` is an alias for the same entry point. For remote deployment, replace the loopback URL with a Server address reachable from the client. The Server continues running when the TUI exits. See the [TUI guide](tui.md) to restore a session or diagnose connection issues.

<a name="3-select-a-compiled-plugin-entry-optional"></a>

<a name="compiled-entry"></a>

## 3. Select a compiled plugin entry (optional)

To have an enabled compiled subagent handle conversations, prepare a complete YAML file using [entry mode](integrate/extensions/subagents.md#entry-mode). Start the installed Server with that file.

```bash
.venv/bin/dataagent serve --config /path/to/config.yaml
```

[Connect the TUI](#connect-tui) and send a request to verify it reaches the selected workflow. After switching entries, restart Server and create a new session. For a small SQLite example that uses a compiled entry, run the [SQLite-to-CSV tutorial](sqlite-tutorial.md).

<a name="optional-dependencies"></a>

## Optional dependencies

SQLite, MySQL and PostgreSQL drivers are included as wheel dependencies. Append `[video]`, `[spark]` or `[all]` to the wheel filename as needed:

```bash
uv pip install --python .venv/bin/python "./datagallery_dataagent-0.2.0-py3-none-any.whl[video]"
```

`video` installs video processing dependencies, `spark` installs PySpark, and `all` selects both. Spark/Hive also requires Java 17 or later and the corresponding service configuration. Configure plugins and database connections in the Server YAML.
