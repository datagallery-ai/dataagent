"""Run the SQLite tutorial and verify the CSV written by DataAgent."""

import argparse
import asyncio
import csv
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import mkdtemp

from dataagent import LaunchOptions, create_agent, prepare_runtime

SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    region TEXT NOT NULL,
    amount_cents INTEGER NOT NULL
);
""".strip()
ORDERS = [("north", 1200), ("south", 1700), ("north", 3300), ("south", 600), ("east", 800)]
EXPECTED_CSV = [
    ["region", "total_cents"],
    ["north", "4500"],
    ["south", "2300"],
    ["east", "800"],
]
QUESTION = (
    "Using orders, group by region and sum amount_cents for all orders. "
    "Return exactly two columns named region and total_cents, in that order. "
    "Sort by total_cents descending and region ascending."
)


async def main(env_file: Path | None) -> None:
    directory = Path(mkdtemp(prefix="dataagent-sqlite-")).resolve()
    database = directory / "orders.sqlite"
    schema = directory / "orders-schema.md"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(SCHEMA)
        connection.executemany("INSERT INTO orders (region, amount_cents) VALUES (?, ?)", ORDERS)
    schema.write_text(SCHEMA + "\n", encoding="utf-8")

    os.environ.update(
        DATAAGENT_HOME=str(directory / "home"),
        DATAAGENT_TUTORIAL_SQLITE=str(database),
        DATAAGENT_TUTORIAL_SCHEMA=str(schema),
    )
    runtime = prepare_runtime(
        LaunchOptions(
            config=Path(__file__).with_name("config.example.yaml"),
            env_file=env_file,
            init_home=True,
        )
    )
    async with create_agent(runtime) as agent:
        result = await agent.ainvoke(
            {"messages": [("user", QUESTION)]},
            config={"configurable": {"thread_id": "sqlite-tutorial"}},
        )
    answer = json.loads(result["messages"][-1].content)
    if answer.get("error") or answer.get("result_file_error") or not answer.get("result_file"):
        raise SystemExit(f"DataAgent did not return a CSV: {answer}")

    result_file = Path(answer["result_file"])
    with result_file.open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    if rows != EXPECTED_CSV:
        raise SystemExit(f"Unexpected result in {result_file}: {rows}")

    print(f"SQL: {answer['sql']}")
    print(f"CSV: {result_file}")
    print(result_file.read_text(encoding="utf-8").rstrip())
    print("Verified: 3 region totals")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Env file containing LLM_* settings")
    asyncio.run(main(parser.parse_args().env_file))
