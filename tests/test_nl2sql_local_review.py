"""Optional reviewer uses one isolated model call and never executes SQL."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"


def test_review_sql_identity_includes_explicit_cte_columns():
    sql_key = PythonLoader().load(PLUGIN, "nl2sql_local/tools/review.py:sql_key")
    first = "WITH p(total) AS (SELECT amount FROM payments) SELECT * FROM p"
    second = "WITH p(other_total) AS (SELECT amount FROM payments) SELECT * FROM p"
    assert sql_key(first) != sql_key(second)


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("mode", ["critique", "independent"])
async def test_review_keeps_original_evidence_and_has_one_review_allowance(failure, mode):
    loader = PythonLoader()
    tool = loader.load(PLUGIN, "nl2sql_local/tools/review.py:review_query")
    contract_type = loader.load(PLUGIN, "nl2sql_local/query_control/state.py:QueryContract")
    contract = contract_type(
        metric="Rate", grain="Per school", numerator="free", denominator="enrollment", joins=[]
    )
    calls = []

    class Model:
        def with_structured_output(self, schema, **kwargs):
            self.schema = schema
            return self

        async def ainvoke(self, messages, config):
            calls.append(json.loads(messages[-1].content))
            if failure:
                await asyncio.sleep(1)
            return {
                "parsed": self.schema(
                    sql="SELECT free / enrollment FROM schools",
                    contract=contract.model_dump(),
                    changes=["Return one row per school"],
                ),
                "raw": AIMessage(content=""),
                "parsing_error": None,
            }

    context = SimpleNamespace(
        get_plugin_config=lambda _: {
            "database": {"path": "/unused.sqlite", "db_id": "fixture"},
            "review_sql": True,
            "review_mode": mode,
            "review_timeout_seconds": 0.01 if failure else 45,
        },
        get_model=lambda: Model(),
        redaction_secrets=(),
    )
    original = "Rates of two schools?\nEvidence: free/enrollment"
    state = {
        "messages": [
            HumanMessage(content=original, id="request"),
            SystemMessage(content="Observed schema prefetch: schools(free, enrollment)"),
        ]
    }
    runtime = SimpleNamespace(context=context, state=state, tool_call_id="review1")
    result = await tool.coroutine(
        "SELECT SUM(free)/SUM(enrollment) FROM schools", contract, runtime
    )
    reply = result.update["messages"][0]
    body = json.loads(reply.content)
    assert body["status"] == ("error" if failure else "reviewed"), body
    expected_attempts = 4 if failure else 1
    assert body["attempts"] == len(calls) == expected_attempts
    assert calls[0]["original_request"] == original
    assert body["mode"] == mode
    if mode == "independent":
        assert "draft_sql" not in calls[0] and "proposed_contract" not in calls[0]
        assert "SELECT SUM(free)" not in json.dumps(calls[0])
    else:
        assert calls[0]["draft_sql"] == "SELECT SUM(free)/SUM(enrollment) FROM schools"
    assert "schools(free, enrollment)" in calls[0]["observed_context"][0]
    state["messages"].append(reply)
    runtime.tool_call_id = "review2"
    again = await tool.coroutine("SELECT 999", contract, runtime)
    rejected = json.loads(again.update["messages"][0].content)
    assert rejected["status"] == "error"
    assert rejected["error"]["code"] == "review_budget_exhausted"
    assert len(calls) == expected_attempts
