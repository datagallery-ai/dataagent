import httpx
import pytest

from dataagent.core.errors import DataAgentError
from dataagent.core.flex.nodes.executor import Executor
from dataagent.core.managers.action_manager.base import ErrorType, classify_exception


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


def test_four_fields_only() -> None:
    error = DataAgentError(
        source="config",
        component="semantic-layer",
        fact="SEMANTIC_LAYER.base_url 未配置",
        trace_id="a1b2c3",
    )
    assert error.source == "config"
    assert error.component == "semantic-layer"
    assert error.fact == "SEMANTIC_LAYER.base_url 未配置"
    assert error.trace_id == "a1b2c3"
    assert not hasattr(error, "locator")
    assert not hasattr(error, "detail")
    assert not hasattr(error, "retryable")
    assert not hasattr(error, "http_status")
    assert not hasattr(error, "_max_retries")
    assert not hasattr(error, "message")


def test_actor_and_wire_are_four_fields() -> None:
    error = DataAgentError(
        source="config",
        component="semantic-layer",
        fact="SEMANTIC_LAYER.base_url 未配置",
        trace_id="a1b2c3",
    )
    expected = {
        "source": "config",
        "component": "semantic-layer",
        "fact": "SEMANTIC_LAYER.base_url 未配置",
        "trace_id": "a1b2c3",
    }
    assert error.to_dict() == expected
    assert error.actor_text() == "[config/semantic-layer] SEMANTIC_LAYER.base_url 未配置"
    assert str(error) == "SEMANTIC_LAYER.base_url 未配置"
    assert "code" not in expected
    assert "locator" not in expected
    assert "retryable" not in expected
    assert "http_status" not in expected
    assert "detail" not in expected


def test_constructor_rejects_message_alias() -> None:
    with pytest.raises(TypeError):
        DataAgentError(
            message="子 Agent 7 超时（30s）",
            source="constraint",
            component="subagent",
            trace_id="trace-timeout",
        )


def test_source_fact_is_fallback_when_fact_missing() -> None:
    error = DataAgentError(source="tool", component="semantic-service", trace_id="t")
    assert error.fact == "工具执行失败"
    assert error.source == "tool"


def test_wire_round_trip_keeps_four_fields() -> None:
    original = DataAgentError(
        source="tool",
        component="nl2sql",
        fact="选中 SQL 执行失败：no such table: missing；sql=select * from missing",
        trace_id="trace-sql",
    )
    restored = DataAgentError.from_dict(original.to_dict())
    assert restored.source == "tool"
    assert restored.component == "nl2sql"
    assert restored.fact == original.fact
    assert restored.trace_id == "trace-sql"
    assert "locator" not in original.to_dict()
    assert "detail" not in original.to_dict()


def test_from_dict_restores_four_fields() -> None:
    original = DataAgentError(
        source="constraint",
        component="subagent",
        fact="子 Agent 7 超时（30s）",
        trace_id="trace-subagent",
    )
    restored = DataAgentError.from_dict(original.to_dict())
    assert restored.source == "constraint"
    assert restored.component == "subagent"
    assert restored.fact == original.fact
    assert restored.trace_id == "trace-subagent"


def test_from_dict_ignores_legacy_extra_keys() -> None:
    restored = DataAgentError.from_dict(
        {
            "source": "tool",
            "component": "semantic-service",
            "fact": "语义服务网络失败：GET retrieve",
            "trace_id": "trace-net",
            "locator": {"http": {"method": "GET"}},
            "detail": {"token": "abc"},
            "retryable": True,
            "http_status": 502,
        }
    )
    assert restored.source == "tool"
    assert restored.fact == "语义服务网络失败：GET retrieve"
    assert restored.to_dict() == {
        "source": "tool",
        "component": "semantic-service",
        "fact": "语义服务网络失败：GET retrieve",
        "trace_id": "trace-net",
    }


def test_executor_retry_matches_raw_exception_type_table() -> None:
    executor = Executor("executor")

    timeout = TimeoutError("deadline")
    assert executor._max_retries_for(timeout) == 1
    assert classify_exception(timeout)[0] == ErrorType.TIMEOUT
    assert executor._should_retry(timeout) is True

    rate = _http_status_error(429)
    assert executor._max_retries_for(rate) == 3
    assert classify_exception(rate)[0] == ErrorType.RATE_LIMIT

    network = ConnectionError("连接失败")
    assert executor._max_retries_for(network) == 3
    assert classify_exception(network)[0] == ErrorType.NETWORK_ERROR

    request_error = httpx.ConnectError("连接失败")
    assert classify_exception(request_error)[0] == ErrorType.NETWORK_ERROR
    assert "network" not in type(request_error).__name__.lower()

    auth = _http_status_error(401)
    assert executor._max_retries_for(auth) == 0
    assert classify_exception(auth)[0] == ErrorType.AUTHENTICATION_ERROR
    assert executor._should_retry(auth) is False

    config = DataAgentError(source="config", fact="SEMANTIC_LAYER.base_url 未配置")
    assert executor._should_retry(config) is False

    ordinary = DataAgentError(source="tool", fact="bash 执行失败")
    assert classify_exception(ordinary)[0] == ErrorType.UNKNOWN
    assert executor._should_retry(ordinary) is False

    wrapped = DataAgentError.from_exception(Exception("timeout 429 schema"))
    assert wrapped.source == "internal"
    assert classify_exception(wrapped)[0] == ErrorType.UNKNOWN
    assert executor._should_retry(wrapped) is False


def test_executor_backoff_uses_raw_exception_policy() -> None:
    executor = Executor("executor")

    timeout_policy = executor._retry_policy_for(TimeoutError("deadline"))
    assert timeout_policy.backoff_type == "fixed"
    assert timeout_policy.backoff_base == 2.0
    assert executor._calculate_backoff(timeout_policy, 0) == 2.0
    assert executor._calculate_backoff(timeout_policy, 1) == 2.0

    rate_policy = executor._retry_policy_for(_http_status_error(429))
    assert rate_policy.backoff_type == "exponential"
    assert rate_policy.backoff_base == 1.0
    assert executor._calculate_backoff(rate_policy, 0) == 1.0
    assert executor._calculate_backoff(rate_policy, 1) == 2.0

    ordinary_policy = executor._retry_policy_for(RuntimeError("bash 执行失败"))
    assert ordinary_policy.backoff_type == "fixed"
    assert ordinary_policy.backoff_base == 1.0
    assert executor._calculate_backoff(ordinary_policy, 0) == 1.0


def test_classify_exception_treats_401_status_as_authentication() -> None:
    error_type, policy = classify_exception(_http_status_error(401))
    assert error_type == ErrorType.AUTHENTICATION_ERROR
    assert policy.retriable is False
    assert policy.max_retries == 0

    executor = Executor("executor")
    auth = _http_status_error(401)
    auth_policy = executor._retry_policy_for(auth)
    assert auth_policy.error_type == ErrorType.AUTHENTICATION_ERROR
    assert executor._max_retries_for(auth) == 0


def test_unknown_exception_does_not_use_message_classification() -> None:
    error_type, policy = classify_exception(Exception("timeout 429"))
    assert error_type == ErrorType.UNKNOWN
    assert policy.max_retries == 1
    assert Executor("executor")._max_retries_for(Exception("timeout 429 schema")) == 1


def test_from_exception_fact_does_not_use_secret_as_fact() -> None:
    error = DataAgentError.from_exception(RuntimeError("api_key=sk-live-secret token=abc"))
    assert error.source == "internal"
    assert "sk-live-secret" not in error.fact
    assert "sk-live-secret" not in str(error.to_dict())


def test_json_token_fact_is_redacted_but_locatable() -> None:
    error = DataAgentError(
        source="tool",
        component="semantic-service",
        fact='{"token":"sk-live-secret","sql":"select 1"}',
        trace_id="trace-json",
    )
    exported = error.to_dict()["fact"]
    assert "sk-live-secret" not in error.fact
    assert "sk-live-secret" not in exported
    assert "sk-live-secret" not in error.actor_text()
    assert "token" in exported
    assert "select 1" in exported
    assert exported != "工具执行失败"


def test_nested_json_authorization_and_url_query_are_redacted() -> None:
    nested = DataAgentError(
        source="tool",
        component="tool",
        fact='{"headers":{"authorization":"Bearer sk-nested","accept":"json"},"q":"users"}',
    )
    nested_fact = nested.to_dict()["fact"]
    assert "sk-nested" not in nested_fact
    assert "authorization" in nested_fact
    assert "accept" in nested_fact
    assert "json" in nested_fact
    assert "users" in nested_fact

    url = DataAgentError(
        source="tool",
        component="tool",
        fact="failed GET https://api.example.com/v1?api_key=sk-xxx&q=users",
    )
    url_fact = url.to_dict()["fact"]
    assert "sk-xxx" not in url_fact
    assert "api.example.com" in url_fact
    assert "q=users" in url_fact.replace("%3D", "=")


def test_actor_export_keeps_facts_after_redacted_secret() -> None:
    error = DataAgentError(
        source="internal",
        component="subagent",
        fact="RuntimeError: disk full api_key=***；sub_id=9",
        trace_id="t",
    )
    assert "sub_id=9" in error.to_dict()["fact"]
    assert "sk-live-secret" not in error.actor_text()


def test_from_exception_preserves_dataagent_error() -> None:
    original = DataAgentError(
        source="tool",
        component="tool",
        fact="bash 执行失败",
        trace_id="trace-tool",
    )
    assert DataAgentError.from_exception(original) is original
    copied = DataAgentError.from_exception(original, trace_id="trace-new")
    assert copied.fact == "bash 执行失败"
    assert copied.source == "tool"
    assert copied.component == "tool"
    assert copied.trace_id == "trace-new"


def test_timeout_error_maps_to_constraint() -> None:
    error = DataAgentError.from_exception(TimeoutError("deadline"), component="sdk")
    assert error.source == "constraint"
    assert error.component == "sdk"
    assert "TimeoutError" in error.fact
    assert "deadline" in error.fact
    assert isinstance(error.__cause__, TimeoutError)
    executor = Executor("executor")
    assert executor._max_retries_for(TimeoutError("deadline")) == 1
    assert executor._retry_policy_for(TimeoutError("deadline")).backoff_base == 2.0
    assert executor._should_retry(error) is False


def test_ipc_timeout_does_not_synthesize_cause_for_retry() -> None:
    from dataagent.core.swarm.worker_result import build_timeout_result

    wire = build_timeout_result(sub_id=7, parent_session_id="parent", timeout=30).to_dict()["error"]
    restored = DataAgentError.from_dict(wire)
    assert "超时" in restored.fact
    assert "7" in restored.fact
    assert "30" in restored.fact
    assert not isinstance(restored.__cause__, TimeoutError)
    assert classify_exception(restored)[0] == ErrorType.UNKNOWN
    executor = Executor("executor")
    assert executor._should_retry(restored) is False
    assert executor._retry_policy_for(restored).error_type == ErrorType.UNKNOWN


def test_actor_export_redacts_bearer_token_in_fact() -> None:
    error = DataAgentError(
        fact="Authorization: Bearer sk-xxx rejected",
        source="tool",
        component="tool",
        trace_id="trace-bearer",
    )
    assert "sk-xxx" not in error.actor_text()
    assert "sk-xxx" not in str(error.to_dict())
    assert "sk-xxx" not in error.to_dict()["fact"]
    assert "rejected" in error.to_dict()["fact"]


def test_json_alias_secret_keys_are_redacted_but_locatable() -> None:
    error = DataAgentError(
        source="tool",
        component="tool",
        fact=(
            '{"access_token":"sk-access","refresh_token":"sk-refresh","apiKey":"sk-apikey",'
            '"client_secret":"sk-client","id_token":"sk-id","sql":"select 1","q":"users"}'
        ),
    )
    exported = error.to_dict()["fact"]
    assert "sk-access" not in exported
    assert "sk-refresh" not in exported
    assert "sk-apikey" not in exported
    assert "sk-client" not in exported
    assert "sk-id" not in exported
    assert "access_token" in exported
    assert "refresh_token" in exported
    assert "apiKey" in exported
    assert "client_secret" in exported
    assert "id_token" in exported
    assert "select 1" in exported
    assert "users" in exported
    assert exported != "工具执行失败"


def test_authorization_token_scheme_redacts_entire_value() -> None:
    error = DataAgentError(
        source="tool",
        component="tool",
        fact="Authorization: Token sk-token-value rejected",
    )
    exported = error.to_dict()["fact"]
    assert "sk-token-value" not in exported
    assert "Token sk-token-value" not in exported
    assert "rejected" in exported
    assert "Authorization" in exported


def test_python_repr_secret_keys_are_redacted() -> None:
    error = DataAgentError(
        source="tool",
        component="tool",
        fact="{'api_key': 'sk-live-secret', 'sql': 'select 1', 'q': 'users'}",
    )
    exported = error.to_dict()["fact"]
    assert "sk-live-secret" not in error.fact
    assert "sk-live-secret" not in exported
    assert "sk-live-secret" not in error.actor_text()
    assert "api_key" in exported
    assert "select 1" in exported
    assert "users" in exported

    wrapped = DataAgentError.from_exception(RuntimeError({"api_key": "sk-live-secret"}))
    assert "sk-live-secret" not in wrapped.fact
    assert "sk-live-secret" not in wrapped.actor_text()
    assert "sk-live-secret" not in str(wrapped.to_dict())


def test_quoted_assignment_redacts_entire_value() -> None:
    double = DataAgentError(
        source="tool",
        component="tool",
        fact='password="my secret value" rejected',
    )
    double_fact = double.to_dict()["fact"]
    assert "my secret value" not in double_fact
    assert "secret value" not in double_fact
    assert 'secret value"' not in double_fact
    assert "password=***" in double_fact
    assert "rejected" in double_fact
    assert "my secret value" not in double.actor_text()

    single = DataAgentError(
        source="tool",
        component="tool",
        fact="password='my secret value' rejected",
    )
    single_fact = single.to_dict()["fact"]
    assert "my secret value" not in single_fact
    assert "secret value" not in single_fact
    assert "password=***" in single_fact
    assert "rejected" in single_fact

    compact = DataAgentError(
        source="tool",
        component="tool",
        fact="password=my_secret_value rejected",
    )
    compact_fact = compact.to_dict()["fact"]
    assert "my_secret_value" not in compact_fact
    assert "password=***" in compact_fact
    assert "rejected" in compact_fact


def test_url_userinfo_is_redacted() -> None:
    bare = DataAgentError(
        source="tool",
        component="tool",
        fact="failed GET https://alice:p%40ss@example.com/v1",
    )
    bare_fact = bare.to_dict()["fact"]
    assert "p%40ss" not in bare_fact
    assert "p@ss" not in bare_fact
    assert "alice:p" not in bare_fact
    assert "example.com" in bare_fact
    assert "/v1" in bare_fact
    assert "p%40ss" not in bare.actor_text()
    assert "p%40ss" not in str(bare.to_dict())

    with_query = DataAgentError(
        source="tool",
        component="tool",
        fact="failed GET https://alice:p%40ss@example.com/v1?api_key=sk-xxx&q=users",
    )
    query_fact = with_query.to_dict()["fact"]
    assert "p%40ss" not in query_fact
    assert "p@ss" not in query_fact
    assert "sk-xxx" not in query_fact
    assert "example.com" in query_fact
    assert "q=users" in query_fact.replace("%3D", "=")


def test_tool_failure_uses_tool_source() -> None:
    from dataagent.core.managers.action_manager.base import tool_failure

    result = tool_failure(fact="missing required argument 'path'")
    assert result.success is False
    assert result.error is not None
    assert result.error.source == "tool"
    assert "missing required argument 'path'" in result.error.fact
    assert Executor("executor")._max_retries_for(result.error) == 1
