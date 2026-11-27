# First data analysis task with SQLite and CSV

Ask for sales totals by region from five sample orders. The tutorial creates a database, has DataAgent generate and execute SQL, and verifies the CSV that DataAgent writes.

## Prepare the environment

Run the commands from the root of the DataAgent source checkout. You need Python 3.11 or 3.12, uv, and a reachable OpenAI-compatible model service. Prepare the Python environment before the first run.

```bash
uv sync --python 3.12
```

Reuse the repository-root `.env` from the [quick start](../quick-start.md). It needs `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY`. If those model variables are already set in your shell environment, you can omit `--env-file .env`. See [Configuration](configuration.md) for the field definitions.

## Run the task

```bash
uv run --no-sync python examples/sqlite-tutorial/run.py --env-file .env
```

The script creates an independent DataAgent Home, a SQLite file, and a table schema in a system temporary directory. It uses the [Python SDK](integrate/sdk.md) to ask for the sum of `amount_cents` by region from the orders table, sorted by total in descending order. The sample [YAML configuration](../../../examples/sqlite-tutorial/config.example.yaml) selects the built-in NL2SQL entry and supplies the table schema. The model generates SQL and DataAgent executes it over a local, read-only SQLite connection. You can run this example without starting a Server, a web frontend, or a semantic service.

## Verify the result

The script prints the SQL, the actual CSV path, and the CSV contents. The file should contain these four lines. Amounts are in cents.

```csv
region,total_cents
north,4500
south,2300
east,800
```

NL2SQL writes the query result to `outputs/default/sqlite-tutorial/nl2sql/<query_id>.csv` in the Home for this run. The script checks the header, row order, and totals before printing `Verified: 3 region totals`. Every run uses a new temporary directory. Once you have kept the results you need, you can delete the `dataagent-sqlite-*` temporary directory containing the printed file.

If the model call fails, check the base URL and API key. If the CSV differs from the expected result, inspect the reported query result and model settings. See [Troubleshooting](troubleshooting.md). The [example script](../../../examples/sqlite-tutorial/run.py) includes the sample rows and the question sent to the model.
