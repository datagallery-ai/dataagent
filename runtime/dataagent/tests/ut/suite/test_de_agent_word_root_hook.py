"""DDL word-root hook tests without dictionary files or LLM requests."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dataagent.core.suite.builtin_suites.de_agent.hooks import word_roots


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    path = tmp_path / "create_ads_user.sql"
    path.write_text(
        "CREATE TABLE db.ads_user (user_id BIGINT COMMENT '用户') "
        "COMMENT '用户表' PARTITIONED BY (pt_d STRING COMMENT '日期') STORED AS ORC;",
        encoding="utf-8",
    )
    configs = [
        {"name": name, "config": {"csv_path": f"{name}.csv"}}
        for name in ("validate_word_roots",)
    ]
    inv = SimpleNamespace(
        tool_name="write_file",
        tool_call_id="test-call",
        tool_args={"path": str(path)},
        execution=SimpleNamespace(
            success=True, error_text=None, error=None,
            output_text="文件写入成功", original_msg="文件写入成功", frontend_msg="已保存文件",
        ),
        runtime=SimpleNamespace(config_manager=SimpleNamespace(get=lambda key, default: configs)),
    )
    monkeypatch.setattr(word_roots, "validate_identifier_word_roots", AsyncMock(return_value={"pass": True}))
    return inv


def test_checks_table_columns_and_partitions_with_tool_config(invocation):
    asyncio.run(word_roots.ddl_word_roots_post(invocation))
    assert invocation.execution.success
    column_call = word_roots.validate_identifier_word_roots.await_args
    assert column_call.args == (["ads_user", "user_id", "pt_d"],)
    assert column_call.kwargs["_tool_context"].tool_config == {"csv_path": "validate_word_roots.csv"}
    assert column_call.kwargs["_tool_context"].runtime is invocation.runtime
    word_roots.validate_identifier_word_roots.assert_awaited_once()


@pytest.mark.parametrize("mode", ["tool_failed", "no_execution", "insert_file", "alter"])
def test_ignores_unrelated_or_failed_writes(invocation, mode):
    if mode == "tool_failed":
        invocation.execution.success = False
    elif mode == "no_execution":
        invocation.execution = None
    elif mode == "insert_file":
        invocation.tool_args["path"] = "insert_ads_user.sql"
    else:
        from pathlib import Path

        Path(invocation.tool_args["path"]).write_text("ALTER TABLE db.ads_user ADD COLUMNS (age INT);")
    asyncio.run(word_roots.ddl_word_roots_post(invocation))
    word_roots.validate_identifier_word_roots.assert_not_awaited()


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
@pytest.mark.parametrize("definition, expected", [
    ("ADD COLUMNS (user_id STRING COMMENT '用户', user_cnt DECIMAL(18,2) COMMENT '数量')",
     ["user_id", "user_cnt"]),
    ("ADD COLUMN user_id STRING COMMENT '用户'", ["user_id"]),
    ("ADD COLUMNS (attrs STRUCT<nested_field:STRING> COMMENT '属性')", ["attrs"]),
])
def test_alter_add_checks_only_new_top_level_columns(invocation, tool_name, definition, expected):
    from dataagent.actions.tools.hooks.base import ToolHookRunner
    from dataagent.actions.tools.hooks.examples.ddl_hooks import ddl_post

    path = Path(invocation.tool_args["path"]).with_name("alter_existing.sql")
    path.write_text(f"ALTER TABLE legacy.existing_unchecked {definition};", encoding="utf-8")
    invocation.tool_args["path"] = str(path)
    invocation.tool_name = tool_name

    asyncio.run(ToolHookRunner.run_post_hooks([ddl_post, word_roots.ddl_word_roots_post], invocation))

    assert invocation.execution.success
    assert word_roots.validate_identifier_word_roots.await_args.args == (expected,)
    word_roots.validate_identifier_word_roots.assert_awaited_once()


@pytest.mark.parametrize("action", [
    "DROP COLUMN old_col",
    "CHANGE COLUMN old_col new_col STRING",
    "RENAME TO legacy.renamed",
    "ADD PARTITION (pt_d='20261028')",
])
def test_other_alter_actions_do_not_trigger_word_root_validation(invocation, action):
    path = Path(invocation.tool_args["path"]).with_name("alter_existing.sql")
    path.write_text(f"ALTER TABLE legacy.existing_unchecked {action};", encoding="utf-8")
    invocation.tool_args["path"] = str(path)

    asyncio.run(word_roots.ddl_word_roots_post(invocation))

    assert invocation.execution.success
    word_roots.validate_identifier_word_roots.assert_not_awaited()


def test_multiple_alter_statements_deduplicate_added_names(invocation):
    path = Path(invocation.tool_args["path"]).with_name("alter_existing.sql")
    path.write_text(
        "ALTER TABLE legacy.target ADD COLUMNS (user_id STRING);\n"
        "ALTER TABLE legacy.origin ADD COLUMNS (user_id STRING, user_cnt INT);",
        encoding="utf-8",
    )
    invocation.tool_args["path"] = str(path)

    asyncio.run(word_roots.ddl_word_roots_post(invocation))

    assert word_roots.validate_identifier_word_roots.await_args.args == (["user_id", "user_cnt"],)


@pytest.mark.parametrize("unavailable", [False, True])
def test_alter_validation_failure_and_unavailable_have_distinct_status(invocation, unavailable):
    path = Path(invocation.tool_args["path"]).with_name("alter_existing.sql")
    path.write_text("ALTER TABLE legacy.target ADD COLUMNS (usr_id STRING);", encoding="utf-8")
    invocation.tool_args["path"] = str(path)
    word_roots.validate_identifier_word_roots.return_value = {
        "pass": False,
        "names": [] if unavailable else [
            {"name": "usr_id", "pass": False, "failed_roots": [{"english_name": "usr"}]}
        ],
        "error_msg": "词根字典缺失" if unavailable else "",
    }

    asyncio.run(word_roots.ddl_word_roots_post(invocation))

    assert invocation.execution.success is unavailable
    if unavailable:
        assert "未完成" in invocation.execution.original_msg
    else:
        assert '"english_name": "usr"' in invocation.execution.error_text


def test_unmatched_roots_are_returned_to_agent(invocation):
    word_roots.validate_identifier_word_roots.return_value = {
        "pass": False, "names": [{"name": "user_id", "failed_roots": [{"english_name": "id"}]}],
        "error_msg": "",
    }
    asyncio.run(word_roots.ddl_word_roots_post(invocation))
    assert not invocation.execution.success
    assert '"english_name": "id"' in invocation.execution.error_text
    assert invocation.execution.error is not None


@pytest.mark.parametrize("error_msg", ["词根字典缺失", "服务超时", "词根接口未返回 results 数组。"])
def test_unavailable_service_is_not_reported_as_passed(invocation, error_msg):
    word_roots.validate_identifier_word_roots.return_value = {
        "pass": False, "names": [], "error_msg": error_msg,
    }
    asyncio.run(word_roots.ddl_word_roots_post(invocation))
    assert invocation.execution.success
    assert invocation.execution.error_text is None
    assert invocation.execution.error is None
    assert invocation.execution.original_msg.startswith("文件写入成功\n")
    assert invocation.execution.frontend_msg.startswith("已保存文件\n")
    assert error_msg in invocation.execution.original_msg
    assert "DDL 词根校验未完成" in invocation.execution.original_msg
    assert "不得声称已通过" in invocation.execution.original_msg
    assert "不要反复重试" in invocation.execution.original_msg
    assert invocation.execution.output_text == invocation.execution.original_msg


def test_unexpected_validation_exception_preserves_write_success(invocation):
    word_roots.validate_identifier_word_roots.side_effect = RuntimeError("service unavailable")

    asyncio.run(word_roots.ddl_word_roots_post(invocation))

    assert invocation.execution.success
    assert invocation.execution.error is None
    assert "RuntimeError: service unavailable" in invocation.execution.original_msg
    assert "DDL 词根校验未完成" in invocation.execution.frontend_msg


@pytest.mark.parametrize("sql", [
    "CREATE TABLE db.ads_user (user_id BIGINT,",
    "CREATE TABLE db.ads_user (user_id STRING COMMENT '未结束)",
])
def test_parse_failure_requires_ddl_fix_without_root_lookup(invocation, sql):
    from pathlib import Path

    Path(invocation.tool_args["path"]).write_text(sql, encoding="utf-8")
    asyncio.run(word_roots.ddl_word_roots_post(invocation))

    assert not invocation.execution.success
    assert "DDL SQL 解析失败" in invocation.execution.error_text
    assert "请修正 DDL" in invocation.execution.error_text
    assert "词根校验不可用" not in invocation.execution.error_text
    assert invocation.execution.error is not None
    word_roots.validate_identifier_word_roots.assert_not_awaited()
