"""Replay the legacy bio_lab read-only cases through the AgentKit parent and task tool."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures")

_EXEC_SPEC = importlib.util.spec_from_file_location(
    "bio_execution", Path(__file__).with_name("execution.py")
)
_execution = importlib.util.module_from_spec(_EXEC_SPEC)
_EXEC_SPEC.loader.exec_module(_execution)
invoke_with_feedback = _execution.invoke_with_feedback
collect_shell_evidence = _execution.collect_shell_evidence


def install_scenario(home):
    home.mkdir(parents=True, exist_ok=True)


def load_cases():
    spec = importlib.util.spec_from_file_location(
        "bio_lab_cases", FIXTURES / "performance_query_cases.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def select_cases(fixtures, group, numbers=None):
    source = "default" if group == "quick" else group
    if group not in {"quick", "TC", "TC-W", "bad_cases"}:
        raise ValueError("Only quick, TC, TC-W and bad_cases are supported")
    if numbers and any(n < 1 for n in numbers):
        raise ValueError("Case numbers start at 1")
    keys = fixtures.select_query_keys(source, quick=group == "quick", query_numbers=numbers)
    selected = {key: fixtures.QUERY_GROUPS[source][key] for key in keys}
    return selected


def config_for(db, semantic_url=None):
    config = {
        "schema_version": 4,
        "models": {
            "primary": {
                "provider": "openai",
                "name": "$env{LLM_MODEL}",
                "base_url": "$env{LLM_BASE_URL}",
                "api_key": "$env{LLM_API_KEY}",
                "temperature": 0,
            }
        },
        "dataagent": {"model": "primary"},
        "plugins": {
            "enabled": ["nl2sql", "data-qa"],
            "config": {
                "data-qa": {"database": "bio_lab"},
                "nl2sql": {
                    "model": "primary",
                    "database": {
                        "db_id": "bio_lab",
                        "dialect": "sqlite",
                        "path": str(db),
                    },
                    "perceptor": {
                        "schema_mode": "full_schema",
                        "user_sql_rules": str(FIXTURES / "sql_rules_bio_lab.md"),
                    },
                    "generator": {"strategies": ["prompt"], "num_samples": 1},
                    "validator": {"db_explain": True},
                    "reflector": {},
                    "executor": {"limit": -1, "preview_limit": 100},
                    "selector": {},
                },
            },
        },
    }
    if semantic_url:
        config["semantic"] = {"base_url": semantic_url, "timeout_seconds": 180}
    return config


def fixture_handler(data, seen):
    import httpx

    def handler(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        key = (
            "columns:" + request.url.params.get("tableName", "")
            if endpoint == "table-columns-info"
            else endpoint
        )
        seen.append({"endpoint": endpoint, "key": key})
        if key not in data:
            raise AssertionError(f"No semantic fixture for {key}")
        return httpx.Response(200, json=data[key])

    return handler


def message_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))


def collect_evidence(messages, db, *, replay_sql=True, evidence_requirement="sql"):
    calls, evidence, errors = {}, [], []
    answer = ""
    schema_observed = False
    for message in messages:
        kind = getattr(message, "type", None)
        if kind == "ai":
            for call in getattr(message, "tool_calls", []):
                if (
                    call.get("name") == "task"
                    and call.get("args", {}).get("subagent_type") == "nl2sql"
                ):
                    calls[call["id"]] = call
                elif (
                    evidence_requirement == "schema"
                    and call.get("name") == "data-qa__get_ontology_description_tool"
                ):
                    calls[call["id"]] = call
            if not getattr(message, "tool_calls", []):
                answer = message_text(message.content)
        if kind != "tool" or getattr(message, "tool_call_id", None) not in calls:
            continue
        item = {
            "call": calls[message.tool_call_id],
            "reply": message_text(message.content),
        }
        evidence.append(item)
        try:
            payload = json.loads(item["reply"])
            item["payload"] = payload
            if item["call"]["name"] == "data-qa__get_ontology_description_tool":
                item["kind"] = "schema"
                if (
                    getattr(message, "status", None) == "error"
                    or not isinstance(payload, dict)
                    or payload.get("error")
                    or not payload.get("database")
                    or not isinstance(payload.get("tables"), list)
                    or not payload["tables"]
                    or not isinstance(payload.get("columns"), dict)
                    or not any(
                        isinstance(value, dict) and value for value in payload["columns"].values()
                    )
                ):
                    raise ValueError("Ontology returned no successful nonempty schema")
                schema_observed = True
                continue
            if (
                evidence_requirement == "schema"
                and isinstance(payload, dict)
                and payload.get("executed") is False
                and not payload.get("error")
                and getattr(message, "status", None) != "error"
            ):
                # A schema-only task may legitimately decline to execute SQL.
                continue
            if (
                getattr(message, "status", None) == "error"
                or not isinstance(payload, dict)
                or payload.get("error")
                or not payload.get("sql")
                or payload.get("executed") is not True
                or not isinstance(payload.get("row_count"), int)
            ):
                if not replay_sql:
                    item["execution_error"] = "NL2SQL returned no successful executed SQL result"
                    continue
                raise ValueError("NL2SQL returned no successful executed SQL result")
            if not replay_sql:
                # TC-W may mutate the DB after this query. Retain its actual tool result,
                # never substitute rows from the final database for historical evidence.
                if not isinstance(payload.get("columns"), list) or not isinstance(
                    payload.get("rows_preview"), list
                ):
                    raise ValueError("NL2SQL returned no execution-time result preview")
                item.update(
                    columns=payload["columns"],
                    rows=payload["rows_preview"],
                    row_count=payload["row_count"],
                    result_source="execution_time_preview",
                    rows_complete=bool(payload.get("total_known"))
                    and len(payload["rows_preview"]) == payload["row_count"],
                )
                continue
            with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                connection.execute("PRAGMA query_only=ON")
                cursor = connection.execute(payload["sql"])
                item["columns"] = [column[0] for column in cursor.description]
                item["rows"] = [list(row) for row in cursor.fetchall()]
                item["row_count"] = len(item["rows"])
                if payload["row_count"] != item["row_count"]:
                    raise ValueError("NL2SQL row_count differs from read-only replay")
        except json.JSONDecodeError as exc:
            if not replay_sql and getattr(message, "status", None) == "error":
                item["execution_error"] = item["reply"]
            else:
                errors.append(str(exc))
        except (ValueError, TypeError, sqlite3.Error) as exc:
            errors.append(str(exc))
    if evidence_requirement == "schema" and not schema_observed:
        errors.append("Missing successful ontology schema tool reply")
    elif evidence_requirement != "schema" and not evidence:
        errors.append("Missing parent task(subagent_type=nl2sql) tool reply")
    if not answer.strip():
        errors.append("Missing final parent answer")
    return answer, evidence, errors


def rule_for(fixtures, key):
    if key in fixtures.ALL_EXPECTED_ANSWER_ASSERTIONS:
        return fixtures.ALL_EXPECTED_ANSWER_ASSERTIONS[key]
    quick = {
        "count_cells": "CELL",
        "count_viruses": "PSEUDOVIRUS",
        "count_antibodies": "ANTIBODY",
    }
    if key in quick:
        prefix = quick[key]
        return {
            "required_terms": [
                str(getattr(fixtures, f"EXPECTED_{prefix}_COUNT")),
                *sorted(getattr(fixtures, f"EXPECTED_{prefix}_IDS")),
            ]
        }
    return None


def evaluate(answer, evidence, errors, rule):
    if errors:
        return {"status": "failed", "errors": errors}
    if not rule:
        return {
            "status": "requires_manual_review",
            "reason": "No reusable expected-answer rule",
        }
    text = answer.casefold()
    missing = [term for term in rule.get("required_terms", []) if term.casefold() not in text]
    missing_groups = [
        group
        for group in rule.get("required_any_term_groups", [])
        if not any(term.casefold() in text for term in group)
    ]
    soft = [term for term in rule.get("soft_required_terms", []) if term.casefold() not in text]
    details = {
        "missing_terms": missing,
        "missing_groups": missing_groups,
        "missing_soft_terms": soft,
    }
    if rule.get("absence_answer_terms") and not any(
        term.casefold() in text for term in rule["absence_answer_terms"]
    ):
        return {"status": "requires_manual_review", **details}
    sql_rule = rule.get("sql_evidence")
    sql_match = True
    if sql_rule:
        sql_match = False
        for item in evidence:
            if not all(
                term.casefold() in item["payload"]["sql"].casefold()
                for term in sql_rule.get("required_sql_terms", [])
            ):
                continue
            rows = item["rows"]
            index = sql_rule.get("null_column")
            if (sql_rule.get("allow_empty_result", True) and not rows) or (
                isinstance(index, int)
                and rows
                and all(len(row) > index and row[index] is None for row in rows)
            ):
                sql_match = True
    passed = not missing and not missing_groups and sql_match
    return {
        "status": "passed"
        if passed
        else ("failed" if rule.get("blocking", True) else "non_blocking_answer_assertion_failed"),
        "sql_evidence_match": sql_match,
        **details,
    }


def evaluate_answer_assertion(rule, answer, feedback):
    if rule.get("allow_human_feedback_pass") and any(
        event.get("payload", {}).get("kind") == "human_feedback" for event in feedback
    ):
        return {"status": "passed", "basis": "human_feedback"}
    return {**evaluate(answer, [], [], rule), "basis": "answer_assertion"}


def evaluate_case(fixtures, key, answer, evidence, errors, feedback=()):
    rule = rule_for(fixtures, key)
    review = fixtures.TC_NON_BLOCKING_REVIEW_CASES.get(key)
    if not review and not (rule and rule.get("grade_answer_only")):
        return evaluate(answer, evidence, errors, rule)
    result = evaluate_answer_assertion(rule, answer, feedback)
    if review:
        result["manual_review"] = {**review, "blocking": False}
    return result


def install_write_skills(home):
    """Reuse fixture-local scripts, without changing production tools or granting confirmations."""
    names = [
        "create-neutralization-experiment",
        "check-inhibitor-sample-for-neutralization-experiment",
        "assign-researcher-for-neutralization-experiment",
    ]
    for name in names:
        shutil.copytree(FIXTURES / "skills" / name, home / "skills" / name)
    return names


def capture_write_effects(db, spec):
    module_spec = importlib.util.spec_from_file_location(
        "bio_write_assertions", Path(__file__).with_name("write_assertions.py")
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module.capture_tcw_database_effects(db, spec)


def evaluate_write_case(
    fixtures,
    key,
    state,
    db,
    original_hash,
    answer,
    evidence,
    errors,
    feedback=None,
    shell_evidence=None,
):
    feedback = feedback or []
    shell_evidence = shell_evidence or []
    rule = rule_for(fixtures, key)
    effect = capture_write_effects(db, fixtures.TCW_DB_EFFECT_ASSERTIONS[key])
    changed = hashlib.sha256(db.read_bytes()).hexdigest() != original_hash
    calls = [call for msg in state.get("messages", []) for call in getattr(msg, "tool_calls", [])]
    feedback_calls = [
        call for call in calls if call.get("name") == "data-qa__request_human_feedback"
    ]
    interrupts = state.get("__interrupt__", ())
    result = {
        "db_effect": effect,
        "answer_blocking": rule.get("blocking", True),
        "db_effect_blocking": fixtures.TCW_DB_EFFECT_ASSERTIONS[key].get("blocking", True),
        "database_changed": changed,
        "hitl": {
            "expected_responses": fixtures.QUERY_GROUPS["TC-W"][key].get("feedback_responses", []),
            "observed_feedback_calls": feedback_calls,
            "interrupts": [str(item) for item in interrupts],
            "consumed_responses": feedback,
            "available": True,
        },
        "capability_gaps": [],
        "feedback_assisted": any(item.get("consumed") for item in feedback),
    }
    tool_errors = [
        message_text(msg.content)
        for msg in state.get("messages", [])
        if getattr(msg, "type", None) == "tool" and getattr(msg, "status", None) == "error"
    ]
    if tool_errors:
        result["tool_errors"] = tool_errors
    if interrupts:
        result.update(
            status="unsupported",
            reason="Native interrupt cannot be mapped to legacy free-text confirmation without a supported HITL adapter",
        )
        return result
    creating = effect["kind"] == "neutralization_created"
    unexpected_mutation = changed and not creating
    if rule.get("grade_answer_and_db_effect_only"):
        answer_check = evaluate_answer_assertion(rule, answer, feedback)
        result.update(status=answer_check["status"], answer_assertion=answer_check)
        if unexpected_mutation:
            result.update(status="failed", errors=["Unexpected database mutation"])
        elif not effect.get("matched"):
            if result["db_effect_blocking"]:
                result.update(status="failed", errors=["Database effect assertion failed"])
            elif result["status"] == "passed":
                result["status"] = "non_blocking_db_assertion_failed"
        return result
    fatal = [
        error for error in errors if error != "Missing parent task(subagent_type=nl2sql) tool reply"
    ]
    readonly_errors = [
        item.get("payload", {}).get("error", "")
        for item in evidence
        if isinstance(item.get("payload"), dict)
        and "readonly" in str(item["payload"].get("error", "")).casefold()
    ]
    if readonly_errors:
        result.update(
            failure_kind="readonly_write_attempt",
            protection_outcome="readonly_blocked",
            business_precondition_verified=False,
            classification_reason="Database protection blocked a routed write attempt; this does not establish the requested business precondition",
        )
    if fatal or unexpected_mutation:
        result.update(
            status="failed",
            errors=fatal
            or ["Write-side database assertion failed or unexpected database mutation"],
        )
        return result
    if any(
        item.get("exit_code") not in (None, 0)
        or (item.get("exit_code") == 0 and not item.get("successful"))
        for item in shell_evidence
    ):
        result.update(
            status="failed",
            errors=["Shell command failed"],
            shell_evidence=shell_evidence,
        )
        return result
    # Keep failed task replies in the report; only successful executions support the answer.
    evidence = [item for item in evidence if item.get("result_source") == "execution_time_preview"]
    if creating:
        confirmed = False
        confirmation_message_index = -1
        message_indices = {
            getattr(message, "tool_call_id", None): index
            for index, message in enumerate(state.get("messages", []))
            if getattr(message, "type", None) == "tool"
        }
        for item in feedback:
            if not item.get("consumed") or not any(
                word in item.get("question", "") for word in ["创建", "执行", "create"]
            ):
                continue
            response = item.get("answer", "").casefold()
            if any(
                word in response
                for word in [
                    "不要",
                    "不能",
                    "不创建",
                    "拒绝",
                    "不同意",
                    "do not",
                    "don't",
                ]
            ):
                confirmed = False
            elif any(word in response for word in ["确认", "同意", "可以", "yes"]):
                tool_call_id = item.get("payload", {}).get("tool_call_id")
                confirmed = tool_call_id in message_indices
                confirmation_message_index = message_indices.get(tool_call_id, -1)
        writes = [
            item
            for item in shell_evidence
            if item.get("script") == "insert_neutralization_experiment.py"
        ]
        if writes and any(
            item.get("call_message_index", -1) <= confirmation_message_index for item in writes
        ):
            confirmed = False
        result["creation_confirmation_observed"] = confirmed
        if not confirmed:
            result.update(
                status="failed",
                errors=[
                    "No actual creation confirmation was consumed through the feedback interrupt"
                ],
            )
        elif not effect.get("matched"):
            result.update(
                status="failed"
                if fixtures.TCW_DB_EFFECT_ASSERTIONS[key].get("blocking", True)
                else "non_blocking_db_assertion_failed",
                errors=["Expected creation database effect was not found"],
            )
        elif not any(
            item.get("qualifies") and item.get("script") == "insert_neutralization_experiment.py"
            for item in shell_evidence
        ):
            result.update(
                status="requires_manual_review",
                reason="DB creation lacks successful insertion-script tool reply evidence",
            )
        else:
            result.update(evaluate(answer, evidence, [], rule))
        return result
    if not effect.get("matched"):
        result.update(
            status="failed"
            if fixtures.TCW_DB_EFFECT_ASSERTIONS[key].get("blocking", True)
            else "non_blocking_db_assertion_failed",
            errors=["Database effect assertion failed"],
        )
        return result
    lower_answer = answer.casefold()
    capability_refusal = any(
        term in lower_answer
        for term in [
            "只读",
            "read-only",
            "read only",
            "不支持写",
            "没有写入",
            "缺少工具",
            "工具不可用",
            "无法调用",
        ]
    )
    if capability_refusal:
        result.update(
            status="requires_manual_review",
            reason="Capability refusal is not evidence that the business precondition was checked",
        )
        return result
    business_reasons = {
        "TC-W06": ["不存在", "找不到", "未找到", "not found", "does not exist"],
        "TC-W07": ["不存在", "找不到", "未找到", "not found", "does not exist"],
        "TC-W08": ["不足", "归档", "不可用", "archived", "insufficient"],
        "TC-W09": ["不存在", "没有", "找不到", "未找到", "not found"],
        "TC-W12": ["不存在", "有效用户", "无效", "找不到", "未找到", "invalid"],
        "TC-W13": ["不存在", "找不到", "未找到", "not found", "does not exist"],
        "TC-W14": ["不存在", "找不到", "未找到", "not found", "does not exist"],
        "TC-W15": ["不存在", "找不到", "未找到", "not found", "does not exist"],
    }
    if not any(term in lower_answer for term in business_reasons.get(key, [])):
        result.update(
            status="requires_manual_review",
            reason="No explicit business precondition reason in the rejection",
        )
        return result
    answer_check = evaluate(answer, evidence, [], rule)
    result["answer_assertion"] = answer_check
    rejection = any(
        term in answer.casefold()
        for term in [
            "不存在",
            "不能",
            "无法",
            "不允许",
            "不足",
            "未创建",
            "未修改",
            "未删除",
            "不会",
            "拒绝",
            "cannot",
            "not found",
            "does not exist",
            "insufficient",
            "refus",
        ]
    )
    result["explicit_rejection"] = rejection
    if answer_check["status"] != "passed":
        result.update(answer_check)
    elif not rejection:
        result.update(
            status="failed",
            errors=[
                "No matching explicit rejection/clarification; unchanged database alone is not success"
            ],
        )
    elif not evidence and not any(
        item.get("qualifies") and item.get("category") == "database_read" for item in shell_evidence
    ):
        result.update(
            status="requires_manual_review",
            reason="Rejection has no executed database evidence; unavailable write/HITL capabilities may mask the business precondition",
        )
    else:
        result["status"] = "passed"
    return result


DEFAULT_MAX_RETRIES = 3


async def run_case_attempt(args, fixtures, key, case, case_dir, session_id, env_file, data):
    import httpx
    from langgraph.checkpoint.memory import InMemorySaver
    from openai import APITimeoutError

    from dataagent import LaunchOptions, create_agent, prepare_runtime
    from dataagent.semantic_client import SemanticServiceClient

    case_dir.mkdir()
    db = case_dir / "bio_lab.sqlite"
    shutil.copy2(FIXTURES / "data/bio_lab.sqlite", db)
    original_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    if args.group == "TC-W":
        os.environ["USER_SQLITE_PATH"] = str(db)
    config = case_dir / "config.yaml"
    config.write_text(json.dumps(config_for(db, args.semantic_url), ensure_ascii=False, indent=2))
    if getattr(args, "prepare_only", False):
        return {"key": key, "status": "prepared", "query": case["query"]}
    seen = []
    client = None
    feedback = []
    progress = _execution.timeout_callback()
    started = time.monotonic()
    result = {
        "key": key,
        "query": case["query"],
        "semantic_mode": "live" if args.semantic_url else "fixture",
    }
    try:
        if env_file is not None and not env_file.is_file():
            raise FileNotFoundError(f"Model environment file does not exist: {env_file}")
        runtime = prepare_runtime(
            LaunchOptions(cwd=args.output.resolve(), config=config, env_file=env_file)
        )
        client = (
            SemanticServiceClient.from_settings(runtime.settings.semantic)
            if args.semantic_url
            else SemanticServiceClient(
                "https://fixture.invalid",
                transport=httpx.MockTransport(fixture_handler(data, seen)),
            )
        )
        async with create_agent(
            runtime,
            semantic_client=client,
            session=runtime.paths.for_session(session_id),
            checkpointer=InMemorySaver(),
        ) as agent:
            state = await asyncio.wait_for(
                invoke_with_feedback(
                    agent,
                    case["query"],
                    session_id,
                    case.get("feedback_responses", []),
                    feedback,
                    progress=progress,
                ),
                args.timeout,
            )
        answer, evidence, errors = collect_evidence(
            state["messages"],
            db,
            replay_sql=args.group != "TC-W",
            evidence_requirement=(rule_for(fixtures, key) or {}).get("evidence_requirement", "sql"),
        )
        shell_evidence = collect_shell_evidence(
            state["messages"], skill_root=runtime.paths.home / "skills"
        )
        result.update(answer=answer, tool_evidence=evidence, shell_evidence=shell_evidence)
        if errors:
            result["evidence_errors"] = errors
        if args.group == "TC-W":
            result.update(
                evaluate_write_case(
                    fixtures,
                    key,
                    state,
                    db,
                    original_hash,
                    answer,
                    evidence,
                    errors,
                    feedback,
                    shell_evidence,
                )
            )
        else:
            result.update(evaluate_case(fixtures, key, answer, evidence, errors, feedback))
        if state.get("__interrupt__") and args.group != "TC-W":
            result.update(status="failed", errors=["Unexpected HITL interrupt"])
    except APITimeoutError as exc:
        result.update(status="failed", timeout_reason="LLM 调用超时", errors=[str(exc)])
    except TimeoutError as exc:
        reason = "本题执行超时"
        if progress.cancelled_model:
            reason = f"LLM 调用超时（{progress.cancelled_model}，达到单题时限）"
        result.update(status="failed", timeout_reason=reason, errors=[f"TimeoutError: {exc}"])
    except Exception as exc:
        result.update(status="failed", errors=[f"{type(exc).__name__}: {exc}"])
    finally:
        if client:
            await asyncio.to_thread(client.close)
    if args.group == "TC-W" and "db_effect" not in result:
        try:
            result["db_effect"] = capture_write_effects(db, fixtures.TCW_DB_EFFECT_ASSERTIONS[key])
            result["database_changed"] = (
                hashlib.sha256(db.read_bytes()).hexdigest() != original_hash
            )
        except (sqlite3.Error, OSError) as exc:
            result["db_effect_error"] = str(exc)
        result["capability_gaps"] = []
    result.update(
        seconds=time.monotonic() - started,
        semantic_requests=seen,
        feedback_transcript=feedback,
        feedback_assisted=any(item.get("consumed") for item in feedback),
    )
    return result


async def replay(args):
    max_retries = getattr(args, "max_retries", DEFAULT_MAX_RETRIES)
    env_file = args.env_file.resolve() if args.env_file else None
    fixtures = load_cases()
    cases = select_cases(fixtures, args.group, args.case)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["DATAAGENT_HOME"] = str(output / "home")
    (output / "home").mkdir()
    install_scenario(output / "home")
    if args.group == "TC-W":
        install_write_skills(output / "home")
        os.environ["PATH"] = (
            str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
        )
    # Explicit config plus a new home prevents the user's existing plugins/config leaking in.
    data = json.loads((FIXTURES / "config/metavisor_responses.json").read_text())
    results = []
    print(f"[{args.group}] {len(cases)} cases | output={output}", flush=True)
    for index, (key, case) in enumerate(cases.items(), 1):
        print("\n" + "=" * 72)
        print(f"[{args.group} {index}/{len(cases)}] {key} | query={case['query']!r}", flush=True)
        started = time.monotonic()
        attempts = []
        for retry in range(max_retries + 1):
            session_id = f"{key}-retry-{retry}" if retry else key
            case_dir = output / session_id
            result = await run_case_attempt(
                args, fixtures, key, case, case_dir, session_id, env_file, data
            )
            if result["status"] == "prepared":
                break
            reason = result.get("timeout_reason")
            attempts.append(
                {
                    "session_id": session_id,
                    "seconds": result["seconds"],
                    "status": result["status"],
                    "timeout_reason": reason,
                }
            )
            if not reason:
                break
            if retry == max_retries:
                print(f"[{key}] {reason}，已用完 {retry} 次重试", flush=True)
            else:
                print(f"[{key}] {reason}，重试本题 {retry + 1}/{max_retries}", flush=True)
        if attempts:
            result.update(attempts=attempts, seconds=time.monotonic() - started)
        results.append(result)
        (output / "results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str)
        )
        if result["status"] == "prepared":
            continue
        print(f"{key}: {result['status']} ({result['seconds']:.1f}s)", flush=True)
        if result.get("answer"):
            print(f"[{key}] answer: {result['answer'][:200]}", flush=True)
        if result.get("errors"):
            print(result["errors"], flush=True)
    return 1 if any(item["status"] in {"failed", "unsupported"} for item in results) else 0


def print_summary(output, groups):
    totals = Counter()
    cases_by_verdict = {"需要人工复核": [], "判分通过": [], "判分失败": []}
    verdicts = {
        "passed": "判分通过",
        "failed": "判分失败",
        "requires_manual_review": "需要人工复核",
        "unsupported": "不支持",
        "non_blocking_answer_assertion_failed": "答案非阻断断言失败",
        "non_blocking_db_assertion_failed": "数据库非阻断断言失败",
    }
    for group, directory in groups:
        results = json.loads((directory / "results.json").read_text())
        counts = Counter(item["status"] for item in results)
        totals.update(counts)
        for item in results:
            status = item["status"]
            verdict = verdicts.get(status, status)
            if status == "failed" and "answer" not in item:
                verdict = "执行超时" if item.get("timeout_reason") else "执行失败"
            cases_by_verdict.setdefault(verdict, []).append(item["key"])
        print(f"[{group}] {len(results)} 题，通过 {counts['passed']} 题", flush=True)
    total = sum(totals.values())
    rate = totals["passed"] / total if total else 0
    labels = {
        "passed": "通过",
        "failed": "失败",
        "requires_manual_review": "需要人工复核",
        "unsupported": "不支持",
    }
    breakdown = [f"{label} {totals[status]}" for status, label in labels.items()]
    for status in sorted(totals.keys() - labels.keys()):
        breakdown.append(f"{status} {totals[status]}")
    print(
        f"最终统计：共 {total} 题，{'，'.join(breakdown)}，通过率 {rate:.1%} | 结果目录：{output}",
        flush=True,
    )
    for verdict, keys in cases_by_verdict.items():
        print(f"{verdict}（{len(keys)}）：{'、'.join(keys) or '无'}", flush=True)


async def run_groups(args):
    """Default one-command run covers TC and TC-W, with separate isolated homes."""
    if args.group != "all":
        code = await replay(args)
        if not args.prepare_only:
            print_summary(args.output, [(args.group, args.output)])
        return code
    from argparse import Namespace

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    codes = []
    for group in ("TC", "TC-W"):
        options = Namespace(**{**vars(args), "group": group, "output": output / group})
        codes.append(await replay(options))
    if not args.prepare_only:
        print_summary(output, [(group, output / group) for group in ("TC", "TC-W")])
    return max(codes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--group", choices=["all", "quick", "TC", "TC-W", "bad_cases"], default="all"
    )
    parser.add_argument(
        "--case", type=int, nargs="+", help="One-based positions within selected group"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New directory; never overwrites a run",
    )
    parser.add_argument("--env-file", type=Path, help="LLM_MODEL, LLM_BASE_URL, LLM_API_KEY")
    parser.add_argument(
        "--semantic-url",
        help="Opt into real full-schema semantic service; default fixture transport",
    )
    parser.add_argument(
        "--timeout", type=float, default=600, help="Seconds per agent attempt (default: 600)"
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help="Retries after a timeout, excluding the initial attempt (default: 3)",
    )
    parser.add_argument(
        "--summary-only", action="store_true", help="Summarize existing TC and TC-W results"
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Copy fixtures and configs without calling a model",
    )
    args = parser.parse_args()
    if args.summary_only:
        print_summary(args.output, [(group, args.output / group) for group in ("TC", "TC-W")])
        return
    if args.max_retries < 0:
        parser.error("--max-retries must be nonnegative")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.group == "all" and args.case:
        parser.error("--case requires an explicit --group")
    raise SystemExit(asyncio.run(run_groups(args)))


if __name__ == "__main__":
    main()
