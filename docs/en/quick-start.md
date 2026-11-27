# Quick start

Start DataAgent on Linux and send your first question in the Web workbench. This path uses Git, npm, uv, and a reachable model service that provides an OpenAI-compatible API.

For Windows or macOS, follow [manual source deployment](guides/deploy-source-manual.md#manual-prerequisites). For the Server wheel and TUI npm package, see [package deployment and version information](guides/deploy-packages.md#get-artifacts).

For WSL 2, check the [resource recommendations](guides/deploy-source.md#wsl2-sizing) before deploying.

## 1. Deploy on Linux

```bash
git clone https://gitcode.com/datagallery/dataagent.git
cd dataagent
./deploy.sh
```

Follow the prompts. On success, the script prints the workbench URL and leaves Web and Server running in the background. See [Linux one-click deployment](guides/deploy-source-linux.md) for prerequisites and lifecycle commands.

## 2. Configure your model

In the repository's `.env`, set the model name, API base URL, and key for your provider:

```dotenv
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

Apply the configuration with `./deploy.sh restart`. See [Configuration](guides/configuration.md) for the files and settings DataAgent reads.

<a name="run-your-first-question"></a>

## 3. Run your first question

Open the workbench URL printed by the script, usually [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks), and send:

```text
In three sentences, say what you can do right now.
```

A streamed reply confirms the Web workbench can reach the Server and the model can respond. To try the same flow from a terminal, run `./deploy.sh tui` and follow the [TUI guide](guides/tui.md).

Once the model responds, use the [SQLite tutorial](guides/sqlite-tutorial.md) to query sample orders and verify the resulting CSV.

## More deployment details

- <a name="1-prepare-tools-and-download-sources"></a>[Prepare tools and download sources](guides/deploy-source-manual.md#manual-prerequisites)
- <a name="3-configure-the-model-service"></a>[Configure the model for manual deployment](guides/deploy-source-manual.md#manual-model)
- <a name="5-verify-health-and-model-calls"></a>[Verify health and model calls](guides/deploy-source-manual.md#manual-verification)
- <a name="connect-source-frontends"></a>Connect [Web](guides/web.md#connect-server) or [TUI](guides/tui.md#connect-server) to an existing Server
- [Troubleshoot source deployment](guides/troubleshooting.md)
