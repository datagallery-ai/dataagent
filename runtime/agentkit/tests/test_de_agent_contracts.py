# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Exercise metadata boundary and evidence contracts without invoking or faking an LLM."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import HumanMessage

from dataagent.declarations import PluginSpec, SubAgentSpec
from dataagent.extensions.compiler import compile_extensions
from dataagent.extensions.loading import PythonLoader
from dataagent.extensions.run_hooks import RunHook
from dataagent.strict_yaml import read_yaml

PLUGIN = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"


def _context(tmp_path):
    return SimpleNamespace(
        session=SimpleNamespace(outputs=tmp_path),
        semantic_client=None,
        get_plugin_config=lambda _: {},
    )


@pytest.mark.parametrize("types", [("string", "bigint"), ("string", "bigint", "string")])
def test_evidence_order_survives_equal_mtimes_and_repeated_payloads(tmp_path, monkeypatch, types):
    """Filesystem iteration and deduplication must not change which schema was saved last."""
    loader = PythonLoader()
    save = loader.load(PLUGIN, "utils/evidence.py:save_evidence")
    read = loader.load(PLUGIN, "utils/evidence.py:read_evidence")
    project = loader.load(PLUGIN, "utils/evidence.py:model_evidence")
    context = _context(tmp_path)
    paths = []
    for value in types:
        saved = save(context, "schema", "warehouse.events", {"type": value})
        path = Path(saved.get("evidence_path"))
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        paths.append(path)
    glob = Path.glob
    monkeypatch.setattr(
        Path,
        "glob",
        lambda path, pattern: (
            iter(reversed(paths)) if path == tmp_path / "metadata" else glob(path, pattern)
        ),
    )
    assert [record.get("payload", {}).get("type") for record in read(context)] == list(types)
    records = project(context)
    assert records[-1].get("payload") == {"type": types[-1]}
    assert len(records) == len(set(types))


def test_model_evidence_preserves_schema_udf_and_raw_diagnostics(tmp_path):
    """Diagnostic removal must not discard column meanings, UDF signatures or raw evidence."""
    loader = PythonLoader()
    save = loader.load(PLUGIN, "utils/evidence.py:save_evidence")
    project = loader.load(PLUGIN, "utils/evidence.py:model_evidence")
    context = _context(tmp_path)
    payload = {
        "dataAccessPlan": {
            "tables": [
                {
                    "db": "warehouse",
                    "table": "events",
                    "description": "Events",
                    "columns": [{"name": "id"}],
                }
            ]
        },
        "diagnostic": {
            "toolTrace": "backend-private-detail",
            "reasoningTrace": "backend-private-detail",
        },
    }
    saved = save(context, "retrieve", "original query", payload)
    schema = {"warehouse.events.id": {"type": "string", "description": "Opaque device identifier"}}
    save(context, "schema", "warehouse.events", schema)
    udf = {"prototype": "Aggregate(String,int,String)", "args": ["value", "rank", "delimiter"]}
    save(context, "udf", "Aggregate", {**udf, "nested": {"diagnostics": {"toolTrace": "private"}}})
    save(context, "schema", "warehouse.events", schema)
    projected = project(context)
    serialized = json.dumps(projected)
    assert len(projected) == 3
    assert "Opaque device identifier" in serialized and udf.get("prototype") in serialized
    assert "backend-private-detail" not in serialized and "diagnostic" not in serialized
    assert "toolTrace" not in serialized
    assert json.loads(Path(saved.get("evidence_path")).read_text()).get("payload") == payload


def test_legacy_evidence_without_recorded_time_still_loads(tmp_path):
    """Pre-fix session files retain their mtime fallback and raw record shape."""
    read = PythonLoader().load(PLUGIN, "utils/evidence.py:read_evidence")
    folder = tmp_path / "metadata"
    folder.mkdir()
    expected = []
    for index, value in enumerate(("string", "bigint"), start=1):
        record = {"kind": "schema", "query": "warehouse.events", "payload": {"type": value}}
        path = folder / f"schema_{index}.json"
        path.write_text(json.dumps(record))
        os.utime(path, ns=(index * 1_000_000_000, index * 1_000_000_000))
        expected.append(record)
    assert read(_context(tmp_path)) == expected


def test_task_hook_uses_original_query_and_reuses_cache(tmp_path):
    """Different delegated searches share the original-query cache and preserve the task call ID."""
    loader = PythonLoader()
    prepare = loader.load(PLUGIN, "hooks/metadata.py:prepare_recall")
    context = _context(tmp_path)
    original = "Count events from warehouse.events"
    raw_path = tmp_path / "raw.json"
    raw_path.write_text("{}")
    cache = tmp_path / ".semantic" / f"{hashlib.sha256(original.encode()).hexdigest()}.json"
    cache.parent.mkdir()
    cache.write_text(
        json.dumps(
            {
                "query": original,
                "tables": [{"table": "warehouse.events", "description": "Events"}],
                "evidence_path": str(raw_path),
            }
        )
    )
    for description in ["Find the event table", "Check the event table columns"]:
        request = ToolCallRequest(
            tool_call={
                "name": "task",
                "id": "recall-call",
                "args": {"subagent_type": "metadata-recall", "description": description},
            },
            tool=None,
            state={"messages": [HumanMessage(content=original)]},
            runtime=SimpleNamespace(context=context),
        )
        updated = prepare(request, params={})
        assert updated.tool_call.get("id") == "recall-call"
        injected = updated.tool_call.get("args", {}).get("description")
        assert description in injected and original in injected
        assert "<semantic_retrieve_context>" in injected and "warehouse.events" in injected
        assert request.tool_call.get("args", {}).get("description") == description
    assert list(tmp_path.rglob("retrieve_*.json")) == []


def test_recall_declares_read_only_files_and_no_free_semantic_search():
    """Metadata recall opts out of native file writes and unrestricted semantic search."""
    spec = SubAgentSpec.model_validate(read_yaml((PLUGIN / "subagents/metadata-recall.yaml")))
    assert spec.filesystem_tools == ["ls", "read_file"]
    assert len(spec.tools) == 4
    assert "semantic_retrieve" not in spec.tools


def test_subagent_only_tools_do_not_leak_into_root(tmp_path):
    """Child tool resolution must retain references that the root cannot use."""
    (tmp_path / "tools.py").write_text(
        'def inspect():\n    """Inspect one item."""\n    return "value"\n'
    )
    (tmp_path / "prompt.md").write_text("Inspect items.")
    (tmp_path / "child.yaml").write_text(
        json.dumps(
            {
                "name": "inspector",
                "description": "Inspect items",
                "system_prompt_file": "prompt.md",
                "tools": ["inspect"],
                "filesystem_tools": ["ls", "read_file"],
            }
        )
    )
    plugin = PluginSpec(
        id="example",
        tools={"inspect": {"entrypoint": "tools.py:inspect", "scope": "subagents"}},
        subagents=["child.yaml"],
    )
    compiled = compile_extensions([(tmp_path, plugin)])
    assert compiled.get("tools") == []
    child = next(iter(compiled.get("subagents")))
    assert [tool.name for tool in child.get("tools")] == ["example__inspect"]
    assert child.get("filesystem_tools") == ["ls", "read_file"]


@pytest.mark.asyncio
async def test_prepare_input_scopes_each_task_without_changing_session_evidence(tmp_path):
    """Exclude unselected tables even inside a mixed Join response and preserve later tasks."""
    loader = PythonLoader()
    save = loader.load(PLUGIN, "utils/evidence.py:save_evidence")
    prepare = loader.load(PLUGIN, "hooks/nl2sql.py:prepare_input")
    context = _context(tmp_path)
    (tmp_path / "create_temp.ads_probe_ds.sql").write_text(
        "CREATE EXTERNAL TABLE IF NOT EXISTS temp.ads_probe_ds "
        "(row_total BIGINT COMMENT '总行数') COMMENT '计数' "
        "PARTITIONED BY (pt_d STRING COMMENT '日期分区') STORED AS ORC;"
    )
    for table in ("warehouse.events", "warehouse.users", "other.events"):
        save(context, "schema", table, {f"{table}.id": {"value_type": "string"}})
    save(context, "schema", "warehouse.events", {"warehouse.events.id": {"value_type": "bigint"}})
    save(
        context,
        "retrieve",
        "Find source tables",
        {
            "dataAccessPlan": {
                "tables": [
                    {"db": "warehouse", "table": "events"},
                    {"db": "warehouse", "table": "users"},
                    {"db": "other", "table": "events"},
                ]
            }
        },
    )
    save(
        context,
        "joins",
        ["warehouse.events", "warehouse.users", "other.events"],
        [
            {
                "src": "warehouse.events.id@hive",
                "target_column": ["warehouse.users.id@hive", "other.events.id@hive"],
                "rel_evidence": "Known identifier relationship",
            },
            {"src": "other.events.id@hive", "target_column": ["warehouse.users.id@hive"]},
        ],
    )
    save(context, "udf", "count", {"prototype": "CountValues(string)"})
    originals = {path: path.read_bytes() for path in (tmp_path / "metadata").glob("*.json")}
    hook = RunHook("before_run", prepare, {}, context)
    question = "Generate INSERT into temp.ads_probe_ds"
    result = await hook.ainvoke(
        {
            "question": json.dumps(
                {
                    "question": question,
                    "source_table": ["warehouse.events", "warehouse.users", "warehouse.events"],
                }
            )
        }
    )
    schemas = result.get("schema_str")
    evidence = json.loads(result.get("evidence"))
    assert result.get("question") == question
    assert schemas.index("CREATE TABLE `warehouse`.`events`") < schemas.index(
        "CREATE TABLE `warehouse`.`users`"
    )
    assert "`id` bigint" in schemas
    assert "`id` string" in schemas
    assert "Known identifier relationship" in schemas
    assert "FOREIGN KEY" not in schemas
    assert evidence.get("source_table") == ["warehouse.events", "warehouse.users"]
    assert "other.events" not in result.get("evidence") + result.get("schema_str")
    assert "CountValues(string)" in result.get("evidence")
    joins = next(
        record.get("payload")
        for record in evidence.get("semantic")
        if record.get("kind") == "joins"
    )
    assert joins == [
        {
            "src": "warehouse.events.id@hive",
            "target_column": ["warehouse.users.id@hive"],
            "rel_evidence": "Known identifier relationship",
        }
    ]
    other = await hook.ainvoke(
        {"question": json.dumps({"question": question, "source_table": ["other.events"]})}
    )
    assert "CREATE TABLE `other`.`events`" in other.get("schema_str")
    assert "warehouse.events" not in other.get("evidence") + other.get("schema_str")
    assert originals == {path: path.read_bytes() for path in (tmp_path / "metadata").glob("*.json")}


@pytest.mark.parametrize("source_table", [None, [], "warehouse.events", ["events"], [None]])
def test_prepare_input_requires_explicit_valid_source_tables(tmp_path, source_table):
    """An absent or invalid selection must never fall back to all session schemas."""
    prepare = PythonLoader().load(PLUGIN, "hooks/nl2sql.py:prepare_input")
    with pytest.raises(ValueError, match="source_table"):
        prepare(
            {"question": json.dumps({"question": "Generate SQL", "source_table": source_table})},
            SimpleNamespace(context=_context(tmp_path)),
            params={},
        )


def test_prepare_input_rejects_partial_schema_evidence(tmp_path):
    """One available schema must not hide another selected table's missing evidence."""
    loader = PythonLoader()
    save = loader.load(PLUGIN, "utils/evidence.py:save_evidence")
    prepare = loader.load(PLUGIN, "hooks/nl2sql.py:prepare_input")
    context = _context(tmp_path)
    save(context, "schema", "warehouse.events", {"warehouse.events.id": {"value_type": "string"}})
    with pytest.raises(
        ValueError, match=r"Missing source schema evidence for \['warehouse.users'\]"
    ):
        prepare(
            {
                "question": json.dumps(
                    {
                        "question": "Generate SQL",
                        "source_table": ["warehouse.events", "warehouse.users"],
                    }
                )
            },
            SimpleNamespace(context=context),
            params={},
        )
