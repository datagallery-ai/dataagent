from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_openai import ChatOpenAI

from dataagent.config.declarations import SubAgentSpec
from dataagent.config.strict_yaml import read_yaml
from dataagent.extensions.loading import PythonLoader
from dataagent.extensions.subagents import materialize_subagents

ROOT = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"


@pytest.mark.parametrize("directory", [False, True])
def test_rename_session_artifact(tmp_path, directory):
    move = PythonLoader().load(ROOT, "tools/files.py:rename_file")
    source = tmp_path / "old"
    if directory:
        source.mkdir()
        (source / "a").write_text("payload")
    else:
        source.write_text("payload")
    runtime = SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(outputs=tmp_path)))
    result = move("old", "new", runtime)
    assert result["ok"] and not source.exists()
    assert ((tmp_path / "new/a") if directory else (tmp_path / "new")).read_text() == "payload"


def test_rename_rejects_escape_overwrite_and_symlink(tmp_path):
    move = PythonLoader().load(ROOT, "tools/files.py:rename_file")
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "a").write_text("keep")
    (outputs / "b").write_text("other")
    outside = tmp_path / "outside"
    outside.write_text("outside")
    (outputs / "link").symlink_to(outside)
    runtime = SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(outputs=outputs)))
    for source, dest in [("a", "../escaped"), ("a", "b"), ("link", "renamed"), ("missing", "new")]:
        assert move(source, dest, runtime)["ok"] is False
    assert (outputs / "a").read_text() == "keep"
    assert (outputs / "b").read_text() == "other"
    assert outside.read_text() == "outside"


def test_metadata_temperature_uses_private_model_copy():
    model = ChatOpenAI(model="test", api_key="unused", temperature=0.1)
    spec = SubAgentSpec.model_validate(read_yaml(ROOT / "subagents/metadata-recall.yaml"))

    def children(spec):
        return materialize_subagents(
            [(ROOT, "de-agent", spec.model_copy(update={"tools": []}))],
            tool_registry={},
            loader=PythonLoader(),
            skill_sources=[],
            model_resolver=lambda _: model,
        )

    child = children(spec)[0]
    assert child["model"].temperature == 0.4
    assert model.temperature == 0.1
    assert children(spec.model_copy(update={"temperature": 0.8}))[0]["model"].temperature == 0.8
