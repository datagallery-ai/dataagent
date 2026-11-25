"""Run one question.json through the parent and the local SQL subagent."""

import asyncio
import json
import re
import time
from pathlib import Path
from uuid import uuid4

from dataagent import LaunchOptions, create_agent, prepare_runtime


def parse_task_result(content):
    """Accept a JSON reply or fenced JSON; preserve escapes inside JSON strings."""
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].removeprefix("json").strip()
    # Some providers emit literal backslash-n between JSON tokens. Only normalize
    # whitespace outside quoted strings; never unescape SQL or values a second time.
    text = re.sub(
        r'"(?:\\.|[^"\\])*"|\\[nrt]',
        lambda match: {r"\n": "\n", r"\r": "\r", r"\t": "\t"}.get(match[0], match[0]),
        text,
    )
    value = json.loads(text)
    if not isinstance(value, dict) or value.get("status") not in {
        "success",
        "partial",
        "needs_clarification",
        "error",
    }:
        raise ValueError("Task reply is not a result object with a recognized status")
    return value


async def main():
    directory = Path(__file__).resolve().parent
    case = json.loads((directory / "question.json").read_text())
    question = case["question"]
    if case.get("evidence"):
        question += "\n\nEvidence: " + case["evidence"]
    runtime = prepare_runtime(
        LaunchOptions(
            config=directory / "config.example.yaml",
            env_file=directory / ".env",
        )
    )
    session = uuid4().hex
    async with create_agent(runtime) as agent:
        started = time.perf_counter()
        async with asyncio.timeout(1800):
            result = await agent.ainvoke(
                {"messages": [("user", question)]},
                config={"configurable": {"thread_id": session}},
            )
        replies = []
        for message in result["messages"]:
            if message.type != "tool" or message.name != "task":
                continue
            reply = {"raw": message.content}
            try:
                reply["parsed"] = parse_task_result(message.content)
            except (ValueError, TypeError, AttributeError) as error:
                reply["parse_error"] = str(error)
            replies.append(reply)
        print(
            json.dumps(
                {
                    "session": session,
                    "seconds": time.perf_counter() - started,
                    "content": result["messages"][-1].content,
                    "task_results": replies,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
