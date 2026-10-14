"""Wire-contract, error and connection-lifetime tests for the semantic client."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from dataagent.errors import DataAgentError
from dataagent.semantic_client import SemanticServiceClient, normalize_semantic_base_url


@pytest.mark.parametrize(
    "method,args,verb,path",
    [
        ("get_table_list", ("db",), "GET", "advanced-search/table-list"),
        ("get_table_columns_info", ("db.table",), "GET", "advanced-search/table-columns-info"),
        (
            "get_columns_sample_values",
            (["db.table.column"],),
            "GET",
            "advanced-search/column-value-info",
        ),
        ("semantic_retrieve", ("orders",), "POST", "semantic/retrieve"),
        (
            "semantic_search_columns",
            ("db", ["orders"], 3),
            "GET",
            "advanced-search/semantic-search-columns",
        ),
        (
            "get_table_relations_path",
            ("db.a", "db.b"),
            "GET",
            "advanced-search/table-relations-path",
        ),
        (
            "vector_search_column_value",
            ("x", "db", "table", 3),
            "GET",
            "advanced-search/vector-search-column-value",
        ),
        ("sql_few_shots", ("orders", 3), "GET", "advanced-search/sql-few-shots"),
        (
            "vector_search_table_desc",
            ("db", ["orders"], 3),
            "GET",
            "advanced-search/vector-search-table-desc",
        ),
        ("get_joinable_tables", (["db.a", "db.b"],), "GET", "advanced-search/joinable-tables"),
        ("search_fulltext", ("orders",), "GET", "search/fulltext"),
        ("search_basic", ({"query": "orders"},), "POST", "search/basic"),
        ("search_dsl", ("orders",), "GET", "search/dsl"),
        (
            "get_entity_by_unique_attribute",
            ("table", "qualifiedName", "db.table"),
            "GET",
            "entity/uniqueAttribute/type/table",
        ),
        ("get_entity_by_guid", ("guid",), "GET", "entity/guid/guid"),
        ("list_retrieval_tables", (), "GET", "retrieval/tables"),
        ("get_retrieval_table_schema", ("table",), "GET", "retrieval/tables/table/schema"),
        ("hybrid_table_columns", (["db.a", "db.b"],), "POST", "hybrid/table-columns"),
    ],
)
def test_all_semantic_endpoints(method, args, verb, path):
    """Preserve all eighteen public endpoint paths and HTTP methods."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with SemanticServiceClient(
        "http://semantic.test/api", transport=httpx.MockTransport(respond)
    ) as client:
        getattr(client, method)(*args)
    (request,) = requests
    assert request.method == verb
    assert request.url.path == f"/api/semantic/v1/{path}"
    assert request.headers.get("accept") == "application/json"


def test_query_encoding_defaults_samples_and_post_payload():
    """Preserve repeated query keys, sample normalization, defaults and request bodies."""
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("column-value-info"):
            return httpx.Response(
                200,
                json={"DB.T.C": {"sample_values": [{"value": 0}, {"value": "x"}, {"value": None}]}},
            )
        return httpx.Response(200, json={})

    with SemanticServiceClient(
        "semantic.test", auth=("user", "password"), transport=httpx.MockTransport(respond)
    ) as client:
        assert client.get_columns_sample_values(["db.t.c"], 3) == {"db.t.c": ["0", "x"]}
        client.get_joinable_tables([" db.a ", "", "db.b"])
        client.get_table_list("db")
        client.get_table_columns_info("db.a")
        client.semantic_retrieve("orders")
        client.hybrid_table_columns([" db.a ", "", "db.b"])
        client.get_entity_by_guid("a/b ?")
    sample, joins, tables, columns, retrieve, hybrid, entity = requests
    assert sample.url.params.get_list("columnNames") == ["db.t.c"]
    assert sample.url.params.get("sampleValuesNumber") == "3"
    assert joins.url.params.get_list("dbTableNames") == ["db.a", "db.b"]
    assert joins.url.params.get("limit") == "2000"
    assert tables.url.params.get("limit") == columns.url.params.get("limit") == "25"
    assert json.loads(retrieve.content) == {"query": "orders"}
    assert json.loads(hybrid.content) == {"tables": ["db.a", "db.b"]}
    assert entity.url.raw_path.endswith(b"a%2Fb%20%3F")
    assert retrieve.headers.get("authorization", "").startswith("Basic ")


@pytest.mark.parametrize(
    "base",
    [
        "semantic.test",
        "http://semantic.test/",
        "http://semantic.test/api",
        "http://semantic.test/api/semantic",
        "http://semantic.test/api/semantic/v1/",
    ],
)
def test_base_url_normalization(base):
    """Accept existing service host and API-prefix configuration forms."""
    assert normalize_semantic_base_url(base) == "http://semantic.test/api/semantic/v1"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>wrong service</html>"),
        httpx.Response(200, content=b"{", headers={"Content-Type": "application/json"}),
        httpx.Response(200, json=True),
    ],
)
def test_invalid_response_is_structured_error(response):
    """Report malformed upstream responses without flattening them to empty data."""
    with SemanticServiceClient(
        "semantic.test", transport=httpx.MockTransport(lambda request: response)
    ) as client:
        with pytest.raises(DataAgentError) as caught:
            client.list_retrieval_tables()
    assert caught.value.component == "semantic-service"
    assert caught.value.to_dict().get("trace_id")


@pytest.mark.parametrize("failure", ["status", "timeout", "connection"])
def test_transport_errors_keep_their_httpx_type(failure, caplog):
    """Keep status and transport categories and sanitize upstream audit messages."""
    expected = {
        "status": httpx.HTTPStatusError,
        "timeout": httpx.ReadTimeout,
        "connection": httpx.ConnectError,
    }

    def respond(request):
        if failure == "status":
            return httpx.Response(
                401, json={"errorCode": "AUTH", "errorMessage": "password=private-value"}
            )
        raise expected.get(failure)("upstream failed", request=request)

    with SemanticServiceClient("semantic.test", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(expected.get(failure)):
            client.list_retrieval_tables()
    assert "private-value" not in caplog.text


def test_close_waits_for_active_request_then_rejects_new_work():
    """Do not close a connection pool underneath an uncancelled worker thread."""
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def respond(request):
        started.set()
        assert release.wait(5)
        return httpx.Response(200, json={"ok": True})

    client = SemanticServiceClient("semantic.test", transport=httpx.MockTransport(respond))

    def close():
        client.close()
        closed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(client.list_retrieval_tables)
        assert started.wait(2)
        closing = pool.submit(close)
        try:
            assert not closed.wait(0.05)
        finally:
            release.set()
        assert request.result(timeout=2) == {"ok": True}
        closing.result(timeout=2)
    assert client.client.is_closed
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        client.list_retrieval_tables()


def test_structured_error_redaction_and_roundtrip():
    """Preserve the legacy four-field error contract without old logger imports."""
    error = DataAgentError(
        source="tool",
        component="semantic-service",
        trace_id="trace",
        fact='{"password": "private", "url": "https://user:secret@test/path?token=hidden"}',
    )
    restored = DataAgentError.from_dict(error.to_dict())
    assert restored.to_dict() == error.to_dict()
    assert restored.actor_text().startswith("[tool/semantic-service]")
    assert all(secret not in str(error) for secret in ("private", "secret", "hidden"))
