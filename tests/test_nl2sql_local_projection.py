"""The contract declares the fields a request asks to see; the runtime compares."""

from pathlib import Path

from conftest import call
from langchain_core.messages import AIMessage
from test_nl2sql_local import CONTRACT, SelectingModel
from test_nl2sql_local import local_db as local_db  # noqa: F401
from test_nl2sql_local_control import PREFIX, run, sql

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
projection = PythonLoader().load(PLUGIN, "nl2sql_local/hooks/projection.py:compare")
names = PythonLoader().load(PLUGIN, "nl2sql_local/hooks/projection.py:projection_names")
describe = PythonLoader().load(PLUGIN, "nl2sql_local/hooks/projection.py:describe")


def test_names_come_from_aliases_columns_and_order():
    sql = "SELECT s.School AS school_name, COUNT(*) AS n, ROUND(1.0*SUM(x)/2, 2) FROM t"
    assert names(sql) == ["school_name", "n", "expression_3"]


def test_declared_single_measure_flags_extra_columns():
    sql = "SELECT p.player_name, p.height, AVG(a.x) AS mean_x FROM p JOIN a ON 1 GROUP BY 1"
    missing, extra, order = projection(["player_name"], sql)
    assert missing == []
    assert extra == ["height", "mean_x"]
    assert order is None


def test_projection_matching_the_declaration_is_clean():
    sql = "SELECT event_name FROM events WHERE ratio = (SELECT MAX(ratio) FROM events)"
    assert projection(["event_name"], sql) == ([], [], None)


def test_word_order_and_separators_do_not_matter():
    sql = "SELECT district_name AS district, ROUND(AVG(x), 2) AS avg_loan FROM t GROUP BY 1"
    assert projection(["district name", "avg_loan"], sql) == ([], [], None)


def test_order_diagnostic_prefers_exact_field_names():
    note = describe(*projection(["date", "release date"], "SELECT release_date, date FROM t"))
    assert "column order" in note
    assert "[release_date, date]" in note and "[date, release date]" in note


def test_ambiguous_aliases_do_not_infer_order():
    assert projection(["a name", "b name"], "SELECT name, name FROM t") == ([], [], None)


def test_missing_field_is_reported_when_the_projection_drops_it():
    sql = "SELECT surname FROM drivers"
    missing, extra, order = projection(["surname", "race_name"], sql)
    assert missing == ["race_name"] and extra == []
    assert order is None


def test_star_projection_is_left_to_the_star_check():
    assert projection(["anything"], "SELECT * FROM t") == ([], [], None)


def test_unparseable_sql_does_not_raise():
    assert projection(["a"], "not sql at all") == ([], [], None)


def test_empty_declaration_never_flags():
    # A contract locked before this field existed must keep working unchanged.
    assert projection([], "SELECT a, b FROM t") == ([], [], None)


def test_description_names_both_sides():
    note = describe(["race_name"], ["q2_time"])
    assert "q2_time" in note and "race_name" in note
    assert describe([], []) is None


def test_guidance_says_computed_is_not_requested():
    assert "computed" in describe([], ["x"]) or "ranking key" in describe([], ["x"])


def declaring(declared):
    """A model that locks the contract with the given output_columns declaration."""

    class Declaring(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            if self.responses[self.cursor].content == "LOCK_CONTRACT":
                self.responses[self.cursor] = call(
                    "nl2sql-react__update_query_state",
                    {"contract": {**CONTRACT, "output_columns": list(declared)}},
                )
            return super()._generate(messages, *args, **kwargs)

    return Declaring


async def test_answer_projection_mismatch_returns_note_without_losing_rows(local_db):  # noqa: F811
    responses = [
        AIMessage(content="LOCK_CONTRACT"),
        call(
            PREFIX + "execute_sql",
            {
                "sql": "SELECT SUM(amount) AS total, COUNT(*) AS n FROM payments",
                "purpose": "answer",
                "contract_id": "CURRENT",
            },
        ),
        AIMessage(content="SELECT_RESULT"),
    ]
    result, model = await run(local_db[0], responses, model_type=declaring(["total"]))
    text = " ".join(
        m.text
        for request in model.requests
        for m in request
        if getattr(m, "name", "") == PREFIX + "execute_sql"
    )
    assert "projection_check" in text and "n" in text
    # The note is advisory: the answer and its rows survive.
    assert result["rows"] == [[117.75, 3]] and result["status"] == "success"


async def test_matching_projection_produces_no_note(local_db):  # noqa: F811
    responses = [
        AIMessage(content="LOCK_CONTRACT"),
        call(
            PREFIX + "execute_sql",
            {
                "sql": "SELECT SUM(amount) AS total FROM payments",
                "purpose": "answer",
                "contract_id": "CURRENT",
            },
        ),
        AIMessage(content="SELECT_RESULT"),
    ]
    result, model = await run(local_db[0], responses, model_type=declaring(["total"]))
    text = " ".join(
        m.text
        for request in model.requests
        for m in request
        if getattr(m, "name", "") == PREFIX + "execute_sql"
    )
    assert "projection_check" not in text
    assert result["status"] in {"success", "partial"}


async def test_reversed_projection_reports_order_without_reordering_result(local_db):  # noqa: F811
    statement = "SELECT COUNT(*) AS n, SUM(amount) AS total FROM payments"
    result, model = await run(
        local_db[0],
        [
            AIMessage(content="LOCK_CONTRACT"),
            sql(statement),
            AIMessage(content="SELECT_RESULT"),
        ],
        model_type=declaring(["total", "n"]),
    )
    reply = next(
        message
        for messages in model.requests
        for message in messages
        if getattr(message, "name", "") == PREFIX + "execute_sql"
    )
    assert "column order" in reply.text
    assert result["columns"] == ["n", "total"]
    assert result["rows"] == [[3, 117.75]]


async def test_contract_field_reaches_the_model(local_db):  # noqa: F811
    responses = [AIMessage(content="LOCK_CONTRACT"), sql(), AIMessage(content="SELECT_RESULT")]
    result, model = await run(local_db[0], responses, model_type=declaring(["total"]))
    text = " ".join(str(m.content) for request in model.requests for m in request)
    assert "output_columns" in text
    assert result["status"] in {"success", "partial"}
