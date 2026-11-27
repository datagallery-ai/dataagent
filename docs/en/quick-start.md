# Quick start

Start DataAgent on Linux and send your first question in the Web workbench. This path uses Git, npm, uv, and a reachable model service that provides an OpenAI-compatible API.

For Windows or macOS, follow [manual source deployment](guides/deploy-source.md#1-prepare-tools-and-download-sources). If you have the Server wheel or TUI npm package, follow [package deployment](guides/deploy-packages.md).

## 1. Deploy on Linux

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

Follow the prompts. On success, the script prints the workbench URL and leaves Web and Server running in the background. See [source deployment](guides/deploy-source.md) for prerequisites and lifecycle commands.

## 2. Configure your model

In the repository's `.env`, set the model name, API base URL, and key for your provider:

```dotenv
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

Apply the configuration with `./deploy.sh restart`. See [Configuration](reference/configuration-api.md) for the files and settings DataAgent reads.

## 3. Run your first question { #run-your-first-question }

Open the workbench URL printed by the script, usually [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks), and send:

```text
In three sentences, say what you can do right now.
```

A streamed reply confirms the Web workbench can reach the Server and the model can respond. To try the same flow from a terminal, run `./deploy.sh tui` and follow the [TUI guide](guides/tui.md).

## More deployment details

- <a id="1-prepare-tools-and-download-sources"></a>[Prepare tools and download sources](guides/deploy-source.md#1-prepare-tools-and-download-sources)
- <a id="3-configure-the-model-service"></a>[Configure the model for manual deployment](guides/deploy-source.md#3-configure-the-model-service)
- <a id="5-verify-health-and-model-calls"></a>[Verify health and model calls](guides/deploy-source.md#5-verify-health-and-model-calls)
- <a id="connect-source-frontends"></a>[Connect source frontends to an existing Server](guides/deploy-source.md#connect-source-frontends)
- [Troubleshoot source deployment](guides/deploy-source.md#troubleshooting)
