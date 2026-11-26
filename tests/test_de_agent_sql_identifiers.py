# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Quoted targets must agree across SQL checks, filesystem feedback and NL2SQL output."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlglot
from langchain_core.messages import ToolMessage

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"
TARGETS = ["biads.ads_xxx", "`biads`.`ads_xxx`", "biads.`ads_xxx`", "`biads`.ads_xxx"]
DDL = """CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_xxx (
user_id STRING COMMENT '用户', cnt BIGINT COMMENT '计数'
) COMMENT '结果表' PARTITIONED BY (pt_d STRING COMMENT '天分区') STORED AS ORC"""
DML = """INSERT OVERWRITE biads.ads_xxx PARTITION (pt_d='$date')
SELECT user_id, COUNT(*) AS cnt FROM src GROUP BY user_id"""


@pytest.mark.parametrize("target", TARGETS)
def test_target_components_ignore_quoting(target):
    parse = PythonLoader().load(PLUGIN, "utils/sql_identifiers.py:parse_target_table")
    assert parse(DDL.replace("biads.ads_xxx", target), dialect="hive") == ("biads", "ads_xxx")
    assert parse(DML.replace("biads.ads_xxx", target), dialect="spark") == ("biads", "ads_xxx")


@pytest.mark.parametrize("ddl_target", TARGETS)
@pytest.mark.parametrize("dml_target", TARGETS)
def test_checker_agrees_for_all_quoted_target_pairs(ddl_target, dml_target):
    detector = PythonLoader().load(PLUGIN, "utils/sql_rule_detectors.py:RuleDetector")
    findings = detector(PLUGIN / "skills/sql-rules/references").detect(
        {
            "ddl": DDL.replace("biads.ads_xxx", ddl_target),
            "dml": DML.replace("biads.ads_xxx", dml_target),
        }
    )
    assert findings == []


@pytest.mark.parametrize(
    "target",
    ["`other`.`ads_xxx`", "`biads`.`ads_other`", "`biads.ads_xxx`", "catalog.biads.ads_xxx"],
)
def test_checker_still_rejects_different_targets(target):
    detector = PythonLoader().load(PLUGIN, "utils/sql_rule_detectors.py:RuleDetector")
    findings = detector(PLUGIN / "skills/sql-rules/references").detect(
        {
            "ddl": DDL,
            "dml": DML.replace("biads.ads_xxx", target),
        }
    )
    assert any(item.rule_id == "TARGET_MISMATCH" for item in findings)


@pytest.mark.parametrize("target", TARGETS)
def test_write_hook_accepts_quoted_ddl_table(tmp_path, target):
    hook = PythonLoader().load(PLUGIN, "hooks/files.py:check_ddl")
    path = tmp_path / "create_ads_xxx.sql"
    path.write_text(DDL.replace("biads.ads_xxx", target))
    request = SimpleNamespace(
        tool_call={"id": "write", "args": {"file_path": str(path)}},
        runtime=SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(outputs=tmp_path))),
    )
    assert hook(request, ToolMessage(content="written", tool_call_id="write"), params={}) is None


@pytest.mark.parametrize("target", ["`biads`.`BadName`", "`biads`.`wrong_name`"])
def test_write_hook_still_rejects_invalid_actual_table_names(tmp_path, target):
    hook = PythonLoader().load(PLUGIN, "hooks/files.py:check_ddl")
    path = tmp_path / "create_bad.sql"
    path.write_text(DDL.replace("biads.ads_xxx", target))
    request = SimpleNamespace(
        tool_call={"id": "write", "args": {"file_path": str(path)}},
        runtime=SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(outputs=tmp_path))),
    )
    result = hook(request, ToolMessage(content="written", tool_call_id="write"), params={})
    assert result.status == "error"
    assert "IF" not in result.content


@pytest.mark.parametrize("target", TARGETS)
def test_nl2sql_saved_hive_sql_passes_checker(tmp_path, target):
    loader = PythonLoader()
    finish = loader.load(PLUGIN, "hooks/nl2sql.py:finish_output")
    detector = loader.load(PLUGIN, "utils/sql_rule_detectors.py:RuleDetector")
    path = tmp_path / "create_ads_xxx.sql"
    ddl = DDL.replace("biads.ads_xxx", target)
    path.write_text(ddl)
    runtime = SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(outputs=tmp_path)))
    result = finish(
        {"sql": DML.replace("biads.ads_xxx", "`biads`.`ads_xxx`"), "question": "biads.ads_xxx"},
        runtime,
        params={},
    )
    saved = Path(result["artifact_path"]).read_text()
    assert '"biads"' not in saved and "`biads`.`ads_xxx`" in saved
    assert "$date" in saved
    assert detector(PLUGIN / "skills/sql-rules/references").detect({"ddl": ddl, "dml": saved}) == []
    assert sqlglot.parse_one(saved, read="hive").this.name == "ads_xxx"


def test_parser_preserves_component_boundaries_and_original_case():
    parse = PythonLoader().load(PLUGIN, "utils/sql_identifiers.py:parse_target_table")
    assert parse("CREATE TABLE `biads.ads_xxx` (a INT)", dialect="hive") == ("biads.ads_xxx",)
    assert parse(
        "CREATE TABLE `BIADS`.`Ads_Xxx` (a INT)", dialect="hive", normalize_case=False
    ) == ("BIADS", "Ads_Xxx")
    with pytest.raises(ValueError):
        parse("SELECT 1", dialect="hive")
    with pytest.raises(ValueError):
        parse("CREATE TABLE a (x INT); DROP TABLE b", dialect="hive")


@pytest.mark.parametrize(
    "question", ["生成 biads.ads_xxx", "生成 `biads`.`ads_xxx`", "生成 BIADS.`ADS_XXX`"]
)
def test_select_quoted_ddl_among_multiple_targets(tmp_path, question):
    select = PythonLoader().load(PLUGIN, "hooks/nl2sql.py:_select_ddl")
    selected = tmp_path / "create_selected.sql"
    selected.write_text(DDL.replace("biads.ads_xxx", "`biads`.`ads_xxx`"))
    (tmp_path / "create_other.sql").write_text(DDL.replace("biads.ads_xxx", "biads.ads_other"))
    context = SimpleNamespace(session=SimpleNamespace(outputs=tmp_path))
    assert select(context, question) == selected


BAD_DDL = [
    "DROP TABLE IF EXISTS biads.ads_bad; CREATE TABLE biads.ads_bad (id STRING)",
    "CREATE TABLE biads.ads_bad (",
    "INSERT INTO biads.ads_xxx SELECT 1",
    "SELECT 1",
    "",
]


@pytest.mark.parametrize("bad_sql", BAD_DDL)
@pytest.mark.parametrize("bad_name", ["create_a_draft.sql", "create_z_draft.sql"])
def test_named_ddl_ignores_unrelated_invalid_candidates(tmp_path, bad_sql, bad_name):
    loader = PythonLoader()
    select = loader.load(PLUGIN, "hooks/nl2sql.py:_select_ddl")
    finish = loader.load(PLUGIN, "hooks/nl2sql.py:finish_output")
    prepare = loader.load(PLUGIN, "hooks/nl2sql.py:prepare_input")
    save = loader.load(PLUGIN, "utils/evidence.py:save_evidence")
    valid = tmp_path / "create_selected.sql"
    valid.write_text(DDL)
    (tmp_path / bad_name).write_text(bad_sql)
    context = SimpleNamespace(
        session=SimpleNamespace(outputs=tmp_path), get_plugin_config=lambda _: {}
    )
    assert select(context, "生成 biads.ads_xxx") == valid
    save(context, "schema", "db.src", {"db.src.user_id": {"value_type": "string"}})
    prepared = prepare(
        {"question": json.dumps({"question": "生成 biads.ads_xxx", "source_table": ["db.src"]})},
        SimpleNamespace(context=context),
        params={},
    )
    assert json.loads(prepared["evidence"])["target_ddl_path"] == str(valid)
    result = finish(
        {"sql": DML, "question": "生成 biads.ads_xxx"}, SimpleNamespace(context=context), params={}
    )
    assert Path(result["artifact_path"]).is_file()


@pytest.mark.parametrize("bad_sql", BAD_DDL)
def test_single_invalid_candidate_reports_filename(tmp_path, bad_sql):
    select = PythonLoader().load(PLUGIN, "hooks/nl2sql.py:_select_ddl")
    path = tmp_path / "create_broken.sql"
    path.write_text(bad_sql)
    with pytest.raises(ValueError, match="create_broken.sql"):
        select(SimpleNamespace(session=SimpleNamespace(outputs=tmp_path)), "生成 biads.ads_xxx")


def test_invalid_candidate_does_not_make_unnamed_selection_unambiguous(tmp_path):
    select = PythonLoader().load(PLUGIN, "hooks/nl2sql.py:_select_ddl")
    (tmp_path / "create_selected.sql").write_text(DDL)
    (tmp_path / "create_broken.sql").write_text("CREATE TABLE broken (")
    with pytest.raises(ValueError, match="name exactly one.*create_broken.sql"):
        select(SimpleNamespace(session=SimpleNamespace(outputs=tmp_path)), "生成 SQL")


def test_unreadable_candidate_is_skipped_only_for_named_valid_target(tmp_path, monkeypatch):
    select = PythonLoader().load(PLUGIN, "hooks/nl2sql.py:_select_ddl")
    valid = tmp_path / "create_selected.sql"
    valid.write_text(DDL)
    unreadable = tmp_path / "create_unreadable.sql"
    unreadable.write_text(DDL)
    read = Path.read_text

    def fail(path, *args, **kwargs):
        if path == unreadable:
            raise OSError("read denied")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail)
    context = SimpleNamespace(session=SimpleNamespace(outputs=tmp_path))
    assert select(context, "biads.ads_xxx") == valid
    valid.unlink()
    with pytest.raises(ValueError, match="create_unreadable.sql.*read denied"):
        select(context, "biads.ads_xxx")
