"""Regression coverage for plans surviving Context run boundaries."""

from pathlib import Path

import pytest

from dataagent.core.context.context import ContextFactory, ContextInitOptions
from dataagent.core.context.todolist_manager import TodoListManager
from dataagent.core.flex.utils.planner_prompt_builder import _build_plan_prompt_variables


@pytest.fixture(autouse=True)
def isolated_contexts():
    """Release Context instances around each test to simulate independent requests."""
    ContextFactory.clear_context()
    yield
    ContextFactory.clear_context()


def _context(workspace: Path, run_id: int, *, user_id: str = "u", session_id: str = "s", sub_id: int = 0):
    return ContextFactory.get_context(
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        sub_id=sub_id,
        options=ContextInitOptions(workspace=workspace),
    )


def test_plan_restores_progress_and_planner_prompt_across_runs(tmp_path: Path) -> None:
    """A new run restores both plan text and the first unfinished task from disk."""
    first = _context(tmp_path, 0)
    first.todolist_manager.create_plan(introduction="分析销售", approach="逐月比较", todos=["读取数据", "比较趋势"])
    first.todolist_manager.complete_current_todo()
    assert len(list((tmp_path / ".context").glob("todolist_*.json"))) == 1
    ContextFactory.clear_context()

    second = _context(tmp_path, 1)
    plan = second.todolist_manager.todolist
    assert plan is not None
    assert plan.introduction == "分析销售"
    assert [todo.completed for todo in plan.todos] == [True, False]
    prompt = _build_plan_prompt_variables(second)
    assert prompt.get("has_plan") is True
    assert prompt.get("plan_current_todo") == "比较趋势"
    second.todolist_manager.update_plan(approach="按季度比较")
    ContextFactory.clear_context()

    third = _context(tmp_path, 2)
    assert third.todolist_manager.todolist.approach == "按季度比较"
    assert [todo.completed for todo in third.todolist_manager.todolist.todos] == [True, False]
    third.todolist_manager.complete_current_todo()
    ContextFactory.clear_context()
    assert _build_plan_prompt_variables(_context(tmp_path, 3)).get("plan_all_todos_done") is True


def test_replaced_and_deleted_plan_stay_changed_after_restart(tmp_path: Path) -> None:
    """Replacing todos resets progress and deleting a plan removes the persisted snapshot."""
    first = _context(tmp_path, 0)
    first.todolist_manager.create_plan(introduction="plan", approach="steps", todos=["old"])
    first.todolist_manager.complete_current_todo()
    first.todolist_manager.update_plan(todos=["new"])
    ContextFactory.clear_context()
    second = _context(tmp_path, 1)
    assert [(todo.title, todo.completed) for todo in second.todolist_manager.todolist.todos] == [("new", False)]
    second.todolist_manager.delete_plan()
    ContextFactory.clear_context()
    assert _context(tmp_path, 2).todolist_manager.todolist is None
    assert not list((tmp_path / ".context").glob("todolist_*.json"))


def test_plans_are_isolated_for_shared_workspace_and_subagents(tmp_path: Path) -> None:
    """Only the same user/session main agent inherits a plan across runs."""
    _context(tmp_path, 0).todolist_manager.create_plan(introduction="main", approach="steps", todos=["main task"])
    _context(tmp_path, 0, sub_id=1).todolist_manager.create_plan(
        introduction="child", approach="steps", todos=["child task"]
    )
    ContextFactory.clear_context()
    assert _context(tmp_path, 1, user_id="other").todolist_manager.todolist is None
    assert _context(tmp_path, 1, session_id="other").todolist_manager.todolist is None
    assert _context(tmp_path, 1, sub_id=1).todolist_manager.todolist is None
    assert _context(tmp_path, 0, sub_id=1).todolist_manager.todolist.introduction == "child"
    assert _context(tmp_path, 1).todolist_manager.todolist.introduction == "main"


def test_plan_uses_configured_context_directory(tmp_path: Path) -> None:
    """Plan snapshots honor the same layout setting as trajectory snapshots."""
    options = ContextInitOptions(
        workspace=tmp_path, config={"WORKSPACE_POLICY": {"layout": {"context_dir": "history"}}}
    )
    first = ContextFactory.get_context("u", "s", 0, 0, options=options)
    first.todolist_manager.create_plan(introduction="plan", approach="steps", todos=[])
    assert list((tmp_path / "history").glob("todolist_*.json"))
    assert not (tmp_path / ".context").exists()
    ContextFactory.clear_context()
    assert ContextFactory.get_context("u", "s", 1, 0, options=options).todolist_manager.todolist is not None


@pytest.mark.parametrize("payload", ['{"todos":', '{"introduction": "x", "approach": "y", "todos": [null]}'])
def test_invalid_snapshot_does_not_prevent_context_start(tmp_path: Path, payload: str) -> None:
    """An incomplete or malformed snapshot can be replaced by a fresh plan."""
    path = tmp_path / "plan.json"
    path.write_text(payload, encoding="utf-8")
    manager = TodoListManager(storage_path=path)
    assert manager.todolist is None
    manager.create_plan(introduction="fresh", approach="steps", todos=["task"])
    assert TodoListManager(storage_path=path).todolist.introduction == "fresh"
