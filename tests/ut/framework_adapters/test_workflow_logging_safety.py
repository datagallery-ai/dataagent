from pathlib import Path


def test_workflow_openjiuwen_does_not_log_raw_query_values() -> None:
    source = Path("dataagent/core/framework_adapters/runtime/workflow_openjiuwen.py").read_text(encoding="utf-8")

    assert 'repr(inputs.get("query"' not in source
    assert 'repr(inputs.get("user_query"' not in source
    assert "query_len" in source
    assert "user_query_len" in source
