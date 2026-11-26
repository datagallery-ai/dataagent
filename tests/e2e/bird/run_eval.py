#!/usr/bin/env python3
"""Evaluate BIRD questions with the repository's NL2SQL workflow."""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from outcome_capture import record_model_call

HERE = Path(__file__).resolve().parent
CURRENT = contextvars.ContextVar("bird_case", default=None)
LOGICAL_CALL = contextvars.ContextVar("bird_logical_call", default=None)


def diagnostic_context():
    case = CURRENT.get()
    return {"case_id": case["dir"].name, "logical_call_id": LOGICAL_CALL.get()} if case else {}


SECRETS = []
NODES = ["perceptor", "generator", "bird-validator", "selector"]


def now():
    return datetime.now(timezone.utc).isoformat()


def runtime_fingerprint(root):
    files = sorted(
        path
        for directory in ("dataagent",)
        for path in (root / directory).rglob("*")
        if path.is_file()
        and path.suffix in (".py", ".md", ".json")
        and "__pycache__" not in path.parts
    )
    inventory = "\n".join(
        str(path.relative_to(root)) + ":" + hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    )
    return hashlib.sha256(inventory.encode()).hexdigest()


def clean(value):
    raw = json.dumps(value, ensure_ascii=False, default=str)
    for secret in SECRETS:
        if secret:
            raw = raw.replace(secret, "[REDACTED]")
    return json.loads(raw)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def append(path, value):
    with path.open("a") as file:
        file.write(json.dumps(clean(value), ensure_ascii=False) + "\n")


def log(root, event, **values):
    record = {"time": now(), "event": event, **values}
    append(root / "run.log", record)
    print(json.dumps(clean(record)), flush=True)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--config", type=Path, default=HERE.parents[2] / "examples/nl2sql-bird-workflow.yaml"
    )
    p.add_argument(
        "--dataset", type=Path, required=True, help="BIRD JSON question list, including Gold SQL"
    )
    p.add_argument(
        "--databases", type=Path, required=True, help="Directory containing <db_id>/<db_id>.sqlite"
    )
    p.add_argument("--plan", type=Path, help="Optional partition JSON with a parts list")
    p.add_argument("--part", type=int, default=1)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--admission-dir", type=Path)
    p.add_argument(
        "--fast-workers",
        "--case-workers",
        dest="fast_workers",
        type=int,
        default=2,
        help="Initial question concurrency for both fast and slow phases (adaptive downward)",
    )
    p.add_argument(
        "--case-window-seconds",
        type=float,
        default=900,
        help="Fixed case concurrency error observation window in seconds",
    )
    p.add_argument(
        "--retained-root", type=Path, help="Frozen previous part; preserve all completed cases"
    )
    p.add_argument("--env-file", type=Path)
    p.add_argument(
        "--database-map",
        type=Path,
        required=True,
        help="Verified BIRD db_id -> semantic databaseName mapping",
    )
    p.add_argument("--http-connect-timeout", type=float, default=10)
    p.add_argument("--http-read-timeout", type=float, default=900)
    p.add_argument("--preflight-only", action="store_true")
    return p.parse_args()


def load_selection(args):
    questions = json.loads(args.dataset.read_text(encoding="utf-8-sig"))
    by_id = {q["question_id"]: q for q in questions}
    if not by_id or len(by_id) != len(questions) or any(type(qid) is not int for qid in by_id):
        raise ValueError("Expected a nonempty list of unique integer question IDs")
    for q in questions:
        if not isinstance(q.get("db_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", q["db_id"]):
            raise ValueError("Invalid BIRD database identifier")
        if not all(
            isinstance(q.get(field), str) and q[field].strip() for field in ("question", "SQL")
        ):
            raise ValueError("Missing question or Gold SQL")
    plan = (
        json.loads(args.plan.read_text())
        if args.plan
        else {"parts": [{"part": 1, "fast": list(by_id), "slow": []}]}
    )
    parts = {row["part"]: row for row in plan["parts"]}
    if len(parts) != len(plan["parts"]) or args.part not in parts:
        raise ValueError("Duplicate or missing partition number")
    ids = [qid for part in parts.values() for phase in ("fast", "slow") for qid in part[phase]]
    if (
        any(type(qid) is not int for qid in ids)
        or len(set(ids)) != len(ids)
        or not set(ids) <= by_id.keys()
    ):
        raise ValueError("Partitions must contain unique known integer question IDs")
    part = parts[args.part]
    mapping = json.loads(args.database_map.read_text())
    if not isinstance(mapping, dict) or any(
        not isinstance(v, str) or not v for v in mapping.values()
    ):
        raise ValueError("database-map must contain nonempty string semantic database names")
    selected = {phase: [by_id[int(q)] for q in part[phase]] for phase in ("fast", "slow")}
    for phase in selected.values():
        for q in phase:
            if q["db_id"] not in mapping:
                raise ValueError("Unverified semantic mapping: " + q["db_id"])
            db = args.databases / q["db_id"] / (q["db_id"] + ".sqlite")
            if not db.is_file():
                raise FileNotFoundError(db)
    return plan, selected, mapping


def retain_completed_cases(old_root, root, selected):
    """Copy completed cases verbatim; caller must use a fresh run directory."""
    allowed = {
        f"{q['db_id']}__q{q['question_id']}": q for phase in selected.values() for q in phase
    }
    results, provenance = {}, []
    if old_root is not None:
        old_root = Path(old_root).resolve()
        if not old_root.is_dir() or old_root == root.resolve():
            raise ValueError("retained-root must be an existing different frozen run")
        for path in sorted((old_root / "cases").glob("*/stats.json")):
            key = path.parent.name
            if key not in allowed:
                continue
            stats = json.loads(path.read_text())
            if stats.get("completed") is not True or stats.get("error"):
                continue
            if stats.get("evaluation_mode") == "generator_candidates" or stats.get(
                "staging_salvaged_from_generator"
            ):
                raise ValueError("Full-pipeline resume cannot retain generator-only stats: " + key)
            question = allowed[key]
            if (
                stats.get("db_id") != question["db_id"]
                or stats.get("question_id") != question["question_id"]
            ):
                raise ValueError("Retained case identity mismatch: " + key)
            for field in ("strict_correct", "extra_columns_correct", "error"):
                if field not in stats:
                    raise ValueError("Retained stats missing " + field + ": " + key)
            destination = root / "cases" / key
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(path.parent, destination)
            results[key] = stats
            provenance.append(
                {
                    "case_id": key,
                    "source": str(path.parent),
                    "stats_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    remaining = {
        phase: [q for q in items if f"{q['db_id']}__q{q['question_id']}" not in results]
        for phase, items in selected.items()
    }
    return remaining, results, provenance


def record_perceptor_timeouts(case_admission, directory, case_id):
    """Only explicit perceptor timeout types count; never inspect prompts or SQL."""
    path = directory / "nl2sql_context_dump" / "nodes.jsonl"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(event, dict)
            and event.get("node") == "perceptor"
            and event.get("status") == "error"
        ):
            case_admission.record_semantic_timeout(case_id, event.get("error_type"))


def config_for(template, args, question, case_dir, mapping):
    cfg = copy.deepcopy(template)
    plugin = cfg["plugins"]["config"]["nl2sql"]
    db = question["db_id"]
    plugin["database"].update(db_id=mapping[db], path=str(args.databases / db / (db + ".sqlite")))
    hooks = plugin.setdefault("run_hooks", {}).setdefault("before_run", [])
    hooks.append(
        {
            "entrypoint": "inject_evidence.py:inject_evidence",
            "params": {"case_input": str(case_dir / "input.json")},
        }
    )
    return cfg


def model_value(value):
    """Resolve an environment-backed model setting without writing its value to disk."""
    match = re.fullmatch(r"\$env\{([A-Za-z_][A-Za-z_0-9]*)\}", value)
    return os.environ[match[1]] if match else value


def audited_model_class():
    from langchain_openai import ChatOpenAI

    class AuditedModel(ChatOpenAI):
        async def astream(self, input, config=None, *, stop=None, **kwargs):
            case = CURRENT.get()
            if case is None:
                raise RuntimeError("LLM stream outside case audit")
            if case["calls"] >= case["model_call_budget"]:
                raise RuntimeError("ModelCallBudgetExceeded: logical calls reached limit")
            case["calls"] += 1
            call_id = case["calls"]
            started = time.monotonic()
            append(
                case["dir"] / "model-events.jsonl",
                {
                    "event": "request",
                    "call_id": call_id,
                    "cumulative_calls": call_id,
                    "stream": True,
                    "time": now(),
                },
            )
            usage, finish = None, None
            record_model_call(call_id)
            logical_token = LOGICAL_CALL.set(call_id)
            try:
                async for chunk in super().astream(input, config=config, stop=stop, **kwargs):
                    usage = chunk.usage_metadata or usage
                    finish = chunk.response_metadata.get("finish_reason") or finish
                    yield chunk
                case["responses"] += 1
                append(
                    case["dir"] / "model-events.jsonl",
                    {
                        "event": "response",
                        "call_id": call_id,
                        "elapsed_seconds": time.monotonic() - started,
                        "usage": usage,
                        "finish_reason": finish,
                    },
                )
            except BaseException as exc:
                case["errors"] += 1
                append(
                    case["dir"] / "model-events.jsonl",
                    {
                        "event": "error",
                        "call_id": call_id,
                        "error_type": type(exc).__name__,
                        "elapsed_seconds": time.monotonic() - started,
                    },
                )
                raise
            finally:
                LOGICAL_CALL.reset(logical_token)

        async def ainvoke(self, input, config=None, *, stop=None, **kwargs):
            case = CURRENT.get()
            if case is None:
                raise RuntimeError("LLM call outside case audit")
            if case["calls"] >= case["model_call_budget"]:
                raise RuntimeError("ModelCallBudgetExceeded: logical calls reached limit")
            case["calls"] += 1
            call_id = case["calls"]
            started = time.monotonic()
            append(
                case["dir"] / "model-events.jsonl",
                {
                    "event": "request",
                    "call_id": call_id,
                    "cumulative_calls": call_id,
                    "time": now(),
                },
            )
            record_model_call(call_id)
            logical_token = LOGICAL_CALL.set(call_id)
            try:
                response = await super().ainvoke(input, config=config, stop=stop, **kwargs)
                case["responses"] += 1
                append(
                    case["dir"] / "model-events.jsonl",
                    {
                        "event": "response",
                        "call_id": call_id,
                        "elapsed_seconds": time.monotonic() - started,
                        "usage": response.usage_metadata,
                        "finish_reason": response.response_metadata.get("finish_reason"),
                    },
                )
                return response
            except BaseException as exc:
                case["errors"] += 1
                append(
                    case["dir"] / "model-events.jsonl",
                    {
                        "event": "error",
                        "call_id": call_id,
                        "error_type": type(exc).__name__,
                        "elapsed_seconds": time.monotonic() - started,
                    },
                )
                raise
            finally:
                LOGICAL_CALL.reset(logical_token)

    return AuditedModel


async def main(args):
    if not 1 <= args.fast_workers <= 8:
        raise ValueError("case workers must be between 1 and 8")
    import math

    if not math.isfinite(args.case_window_seconds) or args.case_window_seconds <= 0:
        raise ValueError("case-window-seconds must be finite and positive")
    repository = HERE.parents[2]
    args.databases = args.databases.resolve()
    root = args.run_dir.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("New run directory must be empty; no implicit resume or overwrite")
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    plan, selected, mapping = load_selection(args)
    signatures = {
        k: hashlib.sha256(path.read_bytes()).hexdigest()
        for k, path in {
            "dataset_sha256": args.dataset,
            "config_sha256": args.config,
            "database_map_sha256": args.database_map,
        }.items()
    }
    if args.retained_root:
        previous = json.loads((args.retained_root / "manifest.json").read_text())
        if any(previous.get(k) != v for k, v in signatures.items()):
            raise ValueError("Retained inputs or evaluation configuration differ")
    original_selected = selected
    selected, retained_results, retained_provenance = retain_completed_cases(
        args.retained_root, root, selected
    )
    sys.path.insert(0, str(repository))
    # Product checker validation imports nl2sql after the isolated loader exits.
    sys.path.insert(0, str(repository / "dataagent/plugins/nl2sql"))
    os.environ["DATAAGENT_HOME"] = str(root / "agentkit-home")
    from dotenv import load_dotenv

    if args.env_file:
        load_dotenv(args.env_file, override=True)
    if args.preflight_only:
        for k, v in {
            "BIRD_EVAL_API_KEY": "offline-preflight-placeholder",
            "BIRD_EVAL_BASE_URL": "http://localhost.invalid/v1",
            "BIRD_SEMANTIC_BASE_URL": "http://localhost.invalid",
        }.items():
            os.environ.setdefault(k, v)
    template = yaml.safe_load(args.config.read_text())
    plugin = template["plugins"]["config"]["nl2sql"]
    if plugin["nodes"] != NODES or plugin["selector"].get("shortcut") != 1:
        raise ValueError("Expected the BIRD validator workflow with shortcut=1")
    alias = plugin["model"]
    if alias != template["dataagent"]["model"]:
        raise ValueError("The evaluator requires a shared top-level and plugin model alias")
    model_cfg = template["models"][alias]
    api_key = model_value(model_cfg["api_key"])
    if not api_key:
        raise ValueError("Model credential is empty")
    SECRETS.extend(
        v for k, v in os.environ.items() if v and k.endswith(("PASSWORD", "API_KEY", "TOKEN"))
    )
    SECRETS.append(api_key)
    base_url = model_value(model_cfg["base_url"])
    from outcome_capture import install as install_outcome_capture

    install_outcome_capture()
    from artifact_capture import capture_case, install_plugin_capture_hooks
    from langchain_core.messages import HumanMessage

    import dataagent
    from dataagent import LaunchOptions, create_agent, prepare_runtime

    install_plugin_capture_hooks()  # Patch actual isolated classes before graph construction.
    if not Path(dataagent.__file__).resolve().is_relative_to(repository):
        raise RuntimeError("Runtime source escaped selected AgentKit")
    manifest = {
        "created_at": now(),
        "part": args.part,
        "total": sum(map(len, original_selected.values())),
        "retained": retained_provenance,
        "retained_count": len(retained_results),
        "remaining_count": sum(map(len, selected.values())),
        "phases": {k: len(v) for k, v in original_selected.items()},
        "remaining_phases": {k: len(v) for k, v in selected.items()},
        "parameters": vars(args),
        "runtime_sha256": runtime_fingerprint(repository),
        "partition_sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest()
        if args.plan
        else None,
        **signatures,
        "schema_source": "live Perceptor BIRD schema service, no cached state",
        "fewshot_source": "live Perceptor sql-few-shots topK5",
        "replay_enabled": False,
        "gate": {
            "maximum": 8,
            "window_seconds": 70,
            "window_maximum": 50,
            "spacing": 1.41,
            "penalty_seconds": 1800,
            "statuses": [408, 504],
            "exceptions": "httpx.TimeoutException",
        },
        "workers": {"fast": args.fast_workers, "slow": args.fast_workers},
        "case_admission": {
            "initial_limit": args.fast_workers,
            "minimum": 1,
            "window_seconds": args.case_window_seconds,
            "error_threshold": 3,
            "decision": "halve_on_3_or_more_errors",
            "automatic_increase": False,
            "errors": [
                "HTTP429_all_classifications",
                "HTTP504",
                "HTTP408",
                "httpx.TimeoutException",
                "perceptor_explicit_semantic_timeout",
            ],
            "cancel_inflight_on_reduction": False,
        },
        "whole_case_retries": 0,
        "time_note": "remote wall time may be skewed; durations use monotonic clock",
    }
    write(root / "manifest.json", manifest)

    def prepare(question):
        key = f"{question['db_id']}__q{question['question_id']}"
        directory = root / "cases" / key
        directory.mkdir(parents=True, exist_ok=False)
        # Ground truth stays exclusively in the evaluator, never model inputs.
        write(
            directory / "input.json",
            {k: question.get(k, "") for k in ("question_id", "db_id", "question", "evidence")},
        )
        write(
            directory / "input_provenance.json",
            {
                "schema_injected": False,
                "gold_sql_injected": False,
                "input_mode": "full_live",
                "fewshot_source": "live_perceptor_top5",
            },
        )
        shutil.copyfile(HERE / "inject_evidence.py", directory / "inject_evidence.py")
        write(directory / "config.yaml", config_for(template, args, question, directory, mapping))
        runtime = prepare_runtime(
            LaunchOptions(cwd=directory, config=directory / "config.yaml", init_home=True)
        )
        return key, directory, runtime

    if args.preflight_only:
        if not any(selected.values()):
            write(
                root / "preflight.json",
                {
                    "status": "PREFLIGHT_NOT_NEEDED",
                    "remaining": 0,
                    "retained_count": len(retained_results),
                    "network_requests": 0,
                },
            )
            return
        question = next(q for phase in selected.values() for q in phase)
        key, directory, runtime = prepare(question)
        session, _ = runtime.paths.open_session(key, create=True)
        async with create_agent(runtime, session=session):
            pass
        write(
            root / "preflight.json",
            {
                "status": "PREFLIGHT_OK",
                "network_requests": 0,
                "dataagent_module": str(Path(dataagent.__file__).resolve()),
                "selected": manifest["total"],
            },
        )
        log(root, "PREFLIGHT_OK", selected=manifest["total"])
        return

    import httpx
    from case_admission import CaseAdmission
    from evaluate_sql import compare
    from physical_gate import install_gate

    async with CaseAdmission(
        root, initial_limit=args.fast_workers, window_seconds=args.case_window_seconds
    ) as case_admission:
        install_gate(
            args.admission_dir or root / "admission",
            base_url,
            diagnostic_secrets=tuple(SECRETS),
            diagnostic_context=diagnostic_context,
            attempt_observer=case_admission.on_physical_event,
        )
        timeout = httpx.Timeout(args.http_read_timeout, connect=args.http_connect_timeout)
        Model = audited_model_class()
        options = dict(
            model=model_value(model_cfg["name"]),
            base_url=base_url,
            api_key=api_key,
            temperature=model_cfg.get("temperature"),
            max_tokens=model_cfg.get("max_tokens"),
            max_retries=model_cfg.get("max_retries", 2),
            timeout=timeout,
            use_responses_api=False,
            extra_body=model_cfg.get("extra_body", {}),
            stream_usage=True,
        )
        results, active = dict(retained_results), set()

        def summarize(status, phase):
            values = list(results.values())
            total = len(values)
            strict = sum(x["strict_correct"] for x in values)
            adjusted = sum(x["extra_columns_correct"] for x in values)
            summary = {
                "status": status,
                "phase": phase,
                "updated_at": now(),
                "selected": manifest["total"],
                "completed": total,
                "active": sorted(active),
                "errors": sum(bool(x["error"]) for x in values),
                "case_limit": case_admission.limit,
                "case_initial_limit": case_admission.initial_limit,
                "case_error_window_seconds": case_admission.window_seconds,
                "case_error_threshold": case_admission.error_threshold,
                "strict_correct": strict,
                "adjusted_correct": adjusted,
                "strict_accuracy_completed": strict / total if total else None,
                "reference_accuracy_completed": adjusted / total if total else None,
                "no_runtime_error_completed": sum(not bool(x["error"]) for x in values),
            }
            write(root / "summary.json", summary)
            write(root / "progress.json", summary)

        async with httpx.AsyncClient(timeout=timeout) as http_async_client:
            with httpx.Client(timeout=timeout) as http_client:

                async def run_case(question, phase):
                    key = f"{question['db_id']}__q{question['question_id']}"
                    active.add(key)
                    summarize("RUNNING", phase)
                    started = time.monotonic()
                    audit = {
                        "calls": 0,
                        "responses": 0,
                        "errors": 0,
                        "model_call_budget": plugin["model_call_budget"],
                    }
                    payload, error = {}, None
                    log(root, "START", case=key, phase=phase)
                    try:
                        _, directory, runtime = prepare(question)
                        audit["dir"] = directory
                        token = CURRENT.set(audit)
                        try:
                            model = Model(
                                **options,
                                http_client=http_client,
                                http_async_client=http_async_client,
                            )
                            session, _ = runtime.paths.open_session(key, create=True)
                            async with create_agent(runtime, model=model, session=session) as agent:
                                with capture_case(
                                    directory, case_id=key, secrets=tuple(SECRETS)
                                ) as capture:
                                    response = await agent.ainvoke(
                                        {"messages": [HumanMessage(content=question["question"])]},
                                        config={
                                            "configurable": {"thread_id": key},
                                            "recursion_limit": 100,
                                        },
                                    )
                                    payload = json.loads(response["messages"][-1].content)
                                    capture.record_final(payload)
                            error = payload.get("error")
                        finally:
                            CURRENT.reset(token)
                    except Exception as exc:
                        error = {"type": type(exc).__name__, "message": str(exc)}
                    directory = root / "cases" / key
                    directory.mkdir(parents=True, exist_ok=True)
                    record_perceptor_timeouts(case_admission, directory, key)
                    predicted = str(payload.get("sql") or "")
                    verdict = {
                        "strict_correct": False,
                        "extra_columns_correct": False,
                        "error": error,
                    }
                    if not error:
                        db = args.databases / question["db_id"] / (question["db_id"] + ".sqlite")
                        verdict = await asyncio.to_thread(compare, db, predicted, question["SQL"])
                    write(directory / "agent_result.json", payload)
                    (directory / "predicted.sql").write_text(predicted)
                    stats = {
                        "gold_sha256": hashlib.sha256(question["SQL"].encode()).hexdigest(),
                        "completed": True,
                        "db_id": question["db_id"],
                        "question_id": question["question_id"],
                        "phase": phase,
                        "elapsed_seconds": time.monotonic() - started,
                        "strict_correct": bool(verdict["strict_correct"]),
                        "extra_columns_correct": bool(verdict["extra_columns_correct"]),
                        "error": error or verdict.get("error"),
                        "evaluation": verdict,
                        "llm_requests": audit["calls"],
                        "llm_responses": audit["responses"],
                        "llm_errors": audit["errors"],
                    }
                    write(directory / "stats.json", stats)
                    results[key] = stats
                    active.remove(key)
                    summarize("RUNNING", phase)
                    log(
                        root,
                        "DONE",
                        case=key,
                        correct=stats["strict_correct"],
                        runtime_error=bool(stats["error"]),
                    )

                for phase in ("fast", "slow"):
                    summarize("RUNNING", phase)

                    async def admitted_case(question):
                        await run_case(question, phase)

                    await case_admission.run_items(selected[phase], admitted_case)
                    from aggregate_artifacts import aggregate

                    aggregate(root, root / "artifact-summary")
                summarize("COMPLETED", "done")
                write(root / "results.json", list(results.values()))


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
