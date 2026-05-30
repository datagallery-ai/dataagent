## Installation

DataAgent requires Python `>=3.11`. We recommend using `uv` to manage the environment and dependencies.

## 1. Install uv

If `uv` is not installed yet, install it via the official method or pip:

```bash
python3 -m pip install uv
```

Verify installation:

```bash
uv --version
```

## 2. Install from Source

```bash
git clone https://github.com/datagallery-ai/DataAgent.git
cd dataagent
uv sync
```

To run tests or build documentation:

```bash
uv sync --extra test
uv sync --extra mkdoc
```

Development tools:

```bash
uv sync --group dev
```

## 3. Configure Environment Variables

Copy `.env.example` to `.env` and fill in model API keys, model service URLs, database endpoints, and other environment-specific values.

```bash
cp .env.example .env
```

Which variables are required depends on your YAML configuration. For example, when `MODEL` uses `provider: "bailian"`, provide the corresponding provider API key.

## 4. Verify Installation

```bash
uv run -m dataagent quickstart
```

You can also run an example configuration:

```python
import asyncio
from dataagent.interface.sdk.agent import DataAgent


async def main():
    agent = DataAgent.from_config("dataagent/core/flex/examples/ecommerce_agent.yaml")
    result = await agent.chat("What can you do?")
    print(result["messages"][-1].content)


asyncio.run(main())
```

## 5. Optional Database Services

Some scenarios use Elasticsearch, PostgreSQL, MySQL, or business databases. Deploy these external services only when your configuration enables `MEMORY`, `DATASOURCE`, NL2SQL databases, or specific business tools.

For database deployment and data import, see: [Database Installation Guide](../installation_doc/database_install/database_install.md).

## 6. Build Distribution Packages

To build wheel/sdist artifacts:

```bash
uv build
```

Output is written to `dist/`.
