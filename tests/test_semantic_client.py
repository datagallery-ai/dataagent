"""Wire-contract, error and connection-lifetime tests for the semantic client."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from dataagent.diagnostics.errors import DataAgentError
from dataagent.semantic_client import SemanticServiceClient, normalize_semantic_base_url
from dataagent.settings import SemanticSettings


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
            ("db", ["orders"]),
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
            ("db", "table", "x"),
            "GET",
            "advanced-search/vector-search-column-value",
        ),
        ("sql_few_shots", ("orders",), "GET", "advanced-search/sql-few-shots"),
        (
            "vector_search_table_desc",
            ("db", ["orders"]),
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


def test_scenario_view_tags_requests_and_skips_health():
    """with_scenario() stamps X-Semantic-Scenario on every request except health."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with SemanticServiceClient(
        "http://semantic.test/api", transport=httpx.MockTransport(respond)
    ) as client:
        client.get_table_list("db")
        view = client.with_scenario("business_twin")
        view.get_table_list("db")
        view.search_basic({"query": "orders"})
        view.get("health")
    assert [request.headers.get("X-Semantic-Scenario") for request in requests] == [
        None,
        "business_twin",
        "business_twin",
        None,
    ]


def test_scenario_view_keeps_explicit_headers_and_blank_scenario_is_dropped():
    """An explicit scenario header wins over the view's; a blank scenario sends none."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with SemanticServiceClient(
        "http://semantic.test/api", transport=httpx.MockTransport(respond)
    ) as client:
        view = client.with_scenario("business_twin")
        view.get("search/fulltext", headers={"X-Semantic-Scenario": "traffic_insight"})
        client.with_scenario("  ").get("search/fulltext")
    assert [request.headers.get("X-Semantic-Scenario") for request in requests] == [
        "traffic_insight",
        None,
    ]


@pytest.mark.parametrize("scenario", [123, ["business_twin"]])
def test_scenario_must_be_a_string(scenario):
    """Non-string scenario values fail fast instead of corrupting a header."""
    with pytest.raises(ValueError, match="semantic scenario must be a string"):
        SemanticServiceClient("http://semantic.test/api", scenario=scenario)


def test_constructed_scenario_stamps_requests_and_skips_health():
    """A client built with scenario= (as from_settings does) stamps the header; health is exempt."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with SemanticServiceClient(
        "http://semantic.test/api", transport=httpx.MockTransport(respond), scenario="business_twin"
    ) as client:
        assert client.scenario == "business_twin"
        client.get("search/fulltext")
        client.get("health")
    assert [request.headers.get("X-Semantic-Scenario") for request in requests] == [
        "business_twin",
        None,
    ]


def test_from_settings_carries_the_configured_semantic_scenario():
    """The root semantic.semantic_scenario setting rides the settings-built client."""
    configured = SemanticSettings(base_url="http://semantic.test", semantic_scenario="business_twin")
    with SemanticServiceClient.from_settings(configured) as client:
        assert client.scenario == "business_twin"
    plain = SemanticSettings(base_url="http://semantic.test")
    with SemanticServiceClient.from_settings(plain) as client:
        assert client.scenario is None


def test_scenario_view_borrows_the_pool_and_closing_it_is_a_noop():
    """Views share the owner's pool: view.close() does nothing, owner.close() rejects views."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = SemanticServiceClient(
        "http://semantic.test/api", transport=httpx.MockTransport(respond)
    )
    view = client.with_scenario("business_twin")
    assert view.client is client.client
    view.close()
    assert not client.client.is_closed
    view.get_table_list("db")
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        view.get_table_list("db")


def test_close_waits_for_active_view_request_then_rejects_new_work():
    """The owner's close() also waits for in-flight scenario-view requests."""
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def respond(request):
        assert request.headers.get("X-Semantic-Scenario") == "business_twin"
        started.set()
        assert release.wait(5)
        return httpx.Response(200, json={"ok": True})

    client = SemanticServiceClient("semantic.test", transport=httpx.MockTransport(respond))
    view = client.with_scenario("business_twin")

    def close():
        client.close()
        closed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(view.list_retrieval_tables)
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
        view.list_retrieval_tables()


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
        assert client.get_columns_sample_values(["db.t.c"], sample_count=3) == {
            "db.t.c": ["0", "x"]
        }
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


def test_counts_are_keyword_only_limit_and_tls_verification_is_on_by_default():
    import inspect

    from dataagent import SemanticServiceClient

    for name in (
        "semantic_search_columns",
        "vector_search_column_value",
        "sql_few_shots",
        "vector_search_table_desc",
        "get_table_list",
        "get_table_columns_info",
        "get_joinable_tables",
        "search_fulltext",
        "search_entities",
    ):
        parameter = inspect.signature(getattr(SemanticServiceClient, name)).parameters["limit"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
    init = inspect.signature(SemanticServiceClient.__init__).parameters
    assert init["verify"].default is True and "_shared" not in init
