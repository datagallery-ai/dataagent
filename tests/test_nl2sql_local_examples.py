"""Demonstrations are pinned, cross-database data and never query the database."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"


def setup(tmp_path):
    rows = [
        {
            "db_id": "current",
            "question": "Percentage of active users?",
            "evidence": "",
            "SQL": "SELECT forbidden_current_answer",
        },
        {
            "db_id": "training",
            "question": "What percentage of orders are paid?",
            "evidence": "paid means flag = 1",
            "SQL": "SELECT 100.0 * SUM(flag = 1) / COUNT(*) FROM orders",
        },
        {
            "db_id": "other",
            "question": "Names of cities?",
            "evidence": "",
            "SQL": "SELECT city FROM places",
        },
    ]
    path = tmp_path / "train.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    options = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "max_examples": 1,
    }
    loader = PythonLoader()
    hook = loader.load(PLUGIN, "nl2sql_local/hooks/examples.py:prepare_examples")
    context = SimpleNamespace(
        get_plugin_config=lambda _: {
            "database": {"path": "/unused.sqlite", "db_id": "current"},
            "examples": options,
        }
    )
    return hook, context, options, path


async def test_example_retrieval_excludes_current_database_and_keeps_budget(tmp_path):
    hook, context, _, _ = setup(tmp_path)
    result = await hook(
        {"messages": [HumanMessage(content="Percentage of active users?")]},
        SimpleNamespace(context=context),
        params={},
    )
    content = result["messages"][0].content
    examples = json.loads(content.split("\n", 1)[1])
    assert len(examples) == 1 and examples[0]["database"] == "training"
    assert "forbidden_current_answer" not in content
    assert "untrusted example data" in content and "No example SQL has been executed" in content


async def test_changed_corpus_cannot_reuse_cached_approved_content(tmp_path):
    hook, context, _, path = setup(tmp_path)
    state = {"messages": [HumanMessage(content="Percentage of active users?")]}
    await hook(state, SimpleNamespace(context=context), params={})
    path.write_text(path.read_text() + "\n{}")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        await hook(state, SimpleNamespace(context=context), params={})


@pytest.mark.parametrize(
    "suffix", ["", "\n\nEvidence: active means flag=1", "\n evidence : active means flag=1"]
)
async def test_identical_cross_database_question_is_excluded_with_evidence(tmp_path, suffix):
    hook, context, options, path = setup(tmp_path)
    identical = {
        "db_id": "another_database",
        "question": "Percentage of active users?",
        "evidence": "active means flag=1",
        "SQL": "SELECT forbidden_identical_answer",
    }
    path.write_text(path.read_text() + "\n" + json.dumps(identical))
    options["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = await hook(
        {"messages": [HumanMessage(content="Percentage of active users?" + suffix)]},
        SimpleNamespace(context=context),
        params={},
    )
    assert "forbidden_identical_answer" not in result["messages"][0].content
    assert "orders" in result["messages"][0].content


async def test_examples_default_disabled_without_files_or_models():
    hook = PythonLoader().load(PLUGIN, "nl2sql_local/hooks/examples.py:prepare_examples")
    context = SimpleNamespace(get_plugin_config=lambda _: {"database": {"path": "/unused.sqlite"}})
    assert await hook({}, SimpleNamespace(context=context), params={}) is None
