# Manual source deployment

Use this path on Windows, macOS, or Linux when you manage dependencies and processes separately. Run these commands from the repository root. For managed startup and shutdown, see [Linux one-click deployment](deploy-source-linux.md).

<a name="manual-prerequisites"></a>

## 1. Prepare tools and download sources

Install Git, Node.js 22.12.0 or newer, npm, and uv. The backend needs Python 3.11 or 3.12. Use packages for your operating system and architecture and add the tools to `PATH`.

```bash
git clone https://gitcode.com/datagallery/dataagent.git
cd dataagent
node --version
npm --version
uv --version
```

Install and run the project in the same environment. Install Windows and WSL dependencies separately. To use a preinstalled Python interpreter or custom npm and Python indexes, set the process variables described in [custom mirrors](deploy-mirrors.md) first. In PowerShell, set variables with `$env:NAME = "value"` and use an interpreter path such as `C:\Python311\python.exe`.

For WSL 2, check the [WSL 2 resource sizing](deploy-source.md#wsl2-sizing) before installing dependencies.

<a name="manual-dependencies"></a>

## 2. Install dependencies

```bash
npm install --include=dev
uv sync --no-dev
```

npm installs Web and TUI build dependencies. uv prepares the Python runtime. For Server-only deployments, skip npm installation and frontend builds. Use `uv sync` when you need development tools. Add `--extra <name>` when the deployment needs video processing or another optional capability. See [staged installation](deploy-mirrors.md#staged-installation) for existing environments, mirror changes, and extras.

<a name="manual-model"></a>

## 3. Configure the model service

Copy `.env.example` to `.env` in the repository root, or edit the file if it already exists. The default model uses these three settings. Replace them with values for a compatible model service that the Server can reach.

```dotenv
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

The npm launcher loads the root `.env` into the process environment. You can also use the Home `.env` or variables set in your shell. Restart the Server after model changes. See [Configuration](configuration.md#files-to-edit) for file selection and precedence.

<a name="manual-start"></a>

## 4. Build and start

```bash
npm run build
npm run start
```

`npm run build` builds Web and TUI. `npm run start` starts Web and starts a Server if none is running at the selected address. To use the terminal client, run `npm run start:tui` in a separate terminal.

For Server-only deployments, use the same Python environment. The uv option below loads the root `.env` into the process environment. Omit `--env-file .env` if you use the Home `.env` or have already exported the variables.

```bash
uv run --no-sync --env-file .env dataagent serve
```

For separate processes, see [connect Web to an existing Server](web.md#connect-server) and [connect TUI to an existing Server](tui.md#connect-server). The standard launch commands work for both acceptance testing and persistent service. For workbench access through a domain name, configure a reverse proxy and see [Web origin](web.md#web-origin).

<a name="manual-verification"></a>

## 5. Verify health and model calls

From another terminal, check the actual listen addresses. For Server-only deployments, run the first two checks. If PowerShell resolves `curl` to an alias, use `curl.exe`.

```bash
curl -i http://127.0.0.1:8790/healthz
curl -i http://127.0.0.1:8790/ready
curl -I http://127.0.0.1:3000/data-tasks
```

An HTTP 200 from `/healthz` confirms the Server process is responding. A 200 from `/ready` confirms the conversation engine is ready. For a 503, see [startup diagnostics](troubleshooting.md#startup). Once you can open the workbench, [send your first question](../quick-start.md#run-your-first-question) and verify the model replies.

To use an enabled plugin as the whole graph, see [compiled subagent entry mode](integrate/extensions/subagents.md#entry-mode). For other deployment failures, see [Troubleshooting](troubleshooting.md).
