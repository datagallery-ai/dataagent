from dataagent.core.flex.nodes.planner import Planner


def test_build_error_result_does_not_return_exception_details_to_frontend() -> None:
    planner = Planner.__new__(Planner)
    planner.name = "planner"
    events = []

    result = planner._build_error_result(events.append, RuntimeError("db password is secret"))

    rendered_events = repr(events)
    assert "db password is secret" not in result["error"]
    assert "db password is secret" not in result["messages"].content
    assert "db password is secret" not in rendered_events
    assert "内部处理异常" in result["error"]
