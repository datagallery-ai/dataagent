"""All generation formats preserve the existing SQL preprocessing contract."""

import sqlite3
from types import SimpleNamespace

import pytest

from dataagent.agents.nl2sql.nodes.generator import GeneratorNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import sql_parser, xml_parser
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.llm_manager.adapters import LLMResponse, LLMStreamChunk


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["prompt", "dc", "skeleton", "icl"])
async def test_generated_placeholders_and_whitespace_reach_database(monkeypatch, strategy):
    monkeypatch.setenv("DATAAGENT_CONTEXT_DUMP", "0")
    node = GeneratorNode(num_samples=1)
    raw = "SELECT\u00a0${starttime, -5, yyyyMMdd} AS period, $date AS day -- explanation\n;"
    content = f"```sql\n{raw}\n```" if strategy == "prompt" else f"<result>\n{raw}\n</result>"

    async def stream(_prompts):
        # The adapter's own order: deltas first, then the complete response.
        yield LLMStreamChunk(content=content)
        yield LLMStreamChunk(final_response=LLMResponse(content=content, usage_metadata={}), done=True)

    monkeypatch.setattr(llm_manager, "get_default_llm", lambda: SimpleNamespace(astream=stream))

    candidates = await node.run_strategy(
        strategy,
        {"dialect": "sqlite", "sql_rules": "Keep template placeholders."},
        {"question": "period", "schema": "", "evidence": "", "few_shot_examples": ""},
    )

    assert len(candidates) == 1
    sql, _, origin = candidates[0]
    assert origin == strategy
    assert sql == "SELECT '${starttime, -5, yyyyMMdd}' AS period, '$date' AS day"
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute(sql).fetchall() == [("${starttime, -5, yyyyMMdd}", "$date")]


@pytest.mark.parametrize("parser,wrapper", [(sql_parser, "```sql\n{}\n```"), (xml_parser, "<result>{}</result>")])
def test_existing_quoted_placeholders_remain_executable(parser, wrapper):
    sql = parser(wrapper.format("SELECT '${starttime, -5, yyyyMMdd}', '$date';"))[0]
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute(sql).fetchone() == ("${starttime, -5, yyyyMMdd}", "$date")
