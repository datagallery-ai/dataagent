"""One BIRD workflow with standard/limited defaults and explicit overrides."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dataagent.core.suite.builtin_suites.bird_benchmark import test_bird_e2e as evaluator
from dataagent.core.suite.builtin_suites.bird_benchmark.model_options import normalize_api_base, normalize_extra_body
from dataagent.core.suite.builtin_suites.bird_benchmark.retry_bird_failures import (
    case_key,
    load_results,
    merge_retry_results,
    read_question_ids_file,
    retry_cases,
    summarize_results,
)
from dataagent.utils.runtime_paths import dataagent_home

MODULE = "dataagent.core.suite.builtin_suites.bird_benchmark.run_bird"
PRESETS = {
    "standard": {"workers": 4, "llm_max_concurrency": None, "generator_max_tokens": None, "case_timeout": None},
    "limited": {"workers": 1, "llm_max_concurrency": 1, "generator_max_tokens": 4096, "case_timeout": 1800.0},
}
DEFAULTS = {
    "llm_timeout": 900.0,
    "llm_num_retries": 2,
    "thinking": "omit",
    "enable_thinking": "omit",
    "reasoning_effort": "omit",
    "extra_body": {},
    "semantic_db_prefix": "bird",
    "preprocess": "skip",
    "retry": True,
    "retry_on": "deferred,agent_error",
    "retry_case_timeout": None,
    "retry_generator_max_tokens": None,
    "exclude_known_questions": False,
    "preprocess_thinking": "omit",
    "preprocess_enable_thinking": "omit",
    "preprocess_reasoning_effort": "omit",
    "preprocess_extra_body": {},
    "preprocess_llm_timeout": 900.0,
    "preprocess_llm_num_retries": 2,
    "value_mode": "text_distinct",
    "text_distinct_max_cardinality": 1000,
    "max_values_per_column": 10000,
    "max_values_per_db": 100000,
    "max_yaml_bytes": 9000000,
}
ALIASES = {
    "bird_data_dir": ("BIRD_DATA_DIR",),
    "train_cache": ("TRAIN_CACHE",),
    "api_base": ("DEEPSEEK_BASE_URL", "LLM_BASE_URL"),
    "model": ("BIRD_RUNTIME_MODEL", "BIRD_E2E_MODEL"),
    "semantic_service_url": ("SEMANTIC_SERVICE_URL",),
    "llm_max_concurrency": ("DATAAGENT_LLM_MAX_CONCURRENCY",),
    "preprocess_root": ("SEMANTIC_PREPROCESS_ROOT",),
}
INTS = {
    "workers",
    "llm_max_concurrency",
    "generator_max_tokens",
    "retry_generator_max_tokens",
    "llm_num_retries",
    "preprocess_llm_num_retries",
    "text_distinct_max_cardinality",
    "max_values_per_column",
    "max_values_per_db",
    "max_yaml_bytes",
}
FLOATS = {"case_timeout", "retry_case_timeout", "llm_timeout", "preprocess_llm_timeout"}
OPTIONAL = {
    "llm_max_concurrency",
    "generator_max_tokens",
    "case_timeout",
    "retry_case_timeout",
    "retry_generator_max_tokens",
}
REQUEST_FIELDS = (
    "model",
    "api_base",
    "thinking",
    "enable_thinking",
    "reasoning_effort",
    "extra_body",
    "llm_timeout",
    "llm_num_retries",
)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"1", "true", "yes"}:
        return True
    if str(value).lower() in {"0", "false", "no"}:
        return False
    raise ValueError(f"expected true or false, got {value!r}")


def resolve_options(
    values: dict[str, Any], environ: dict[str, str] | None = None, saved: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve once; explicit none overrides, rather than inherits, a preset."""
    env = os.environ if environ is None else environ
    mode = values.get("mode", env.get("BIRD_MODE", (saved or {}).get("mode", "standard")))
    if mode not in PRESETS:
        raise ValueError("mode must be standard or limited")
    inherited = dict(saved or {})
    if inherited.get("mode", mode) != mode:
        for key in PRESETS[mode]:
            inherited.pop(key, None)
    result = {**DEFAULTS, **PRESETS[mode], **inherited, "mode": mode}
    keys = (
        set(result)
        | set(ALIASES)
        | {"bird_data_dir", "train_cache", "preprocess_root", "preprocess_model", "preprocess_api_base"}
    )
    for key in keys:
        for env_name in ("BIRD_" + key.upper(), *ALIASES.get(key, ())):
            if env_name in env:
                result[key] = env[env_name]
                break
    result.update(
        {
            key: value
            for key, value in values.items()
            if key not in {"command", "api_key_file", "preprocess_api_key_file"}
        }
    )
    for key in INTS | FLOATS:
        value = result.get(key)
        if key in OPTIONAL and (value is None or str(value).lower() in {"none", "unlimited"}):
            result[key] = None
            continue
        number = int(value) if key in INTS else float(value)
        lower = 0 if key.endswith("num_retries") else 1 if key in INTS else 0
        if not math.isfinite(number) or number < lower or (key in FLOATS and number == 0):
            raise ValueError(f"invalid {key}: {value}")
        result[key] = number
    for key in ("retry", "exclude_known_questions"):
        result[key] = _boolean(result[key])
    worker_budgets(result["workers"], result["llm_max_concurrency"])
    for prefix in ("", "preprocess_"):
        extra = result[prefix + "extra_body"]
        if isinstance(extra, str):
            extra = json.loads(extra)
        result[prefix + "extra_body"] = extra
        normalize_extra_body(
            thinking=result[prefix + "thinking"],
            enable_thinking=result[prefix + "enable_thinking"],
            reasoning_effort=result[prefix + "reasoning_effort"],
            extra_body=extra,
        )
        if result.get(prefix + "api_base"):
            result[prefix + "api_base"] = normalize_api_base(result[prefix + "api_base"])
    if result["preprocess"] not in {"skip", "reuse", "prepare"}:
        raise ValueError("preprocess must be skip, reuse or prepare")
    if result["value_mode"] not in {"sample", "text_distinct", "all_distinct"}:
        raise ValueError("value-mode must be sample, text_distinct or all_distinct")
    categories = str(result["retry_on"]).split(",")
    if not set(categories) <= {"deferred", "agent_error", "incorrect"}:
        raise ValueError("retry-on accepts deferred,agent_error,incorrect")
    return result


def worker_budgets(workers: int, total: int | None) -> list[int | None]:
    if workers < 1 or (total is not None and (total < 1 or workers > total)):
        raise ValueError("workers must be positive and no greater than a finite llm-max-concurrency")
    return [None] * workers if total is None else [total // workers + int(i < total % workers) for i in range(workers)]


def _credentials(path: str | None, env: dict[str, str]) -> dict[str, str]:
    output = dict(env)
    if path:
        content = Path(path).read_text(encoding="utf-8").strip()
        parsed = {}
        for line in content.splitlines():
            line = line.strip().removeprefix("export ")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in {"LLM_API_KEY", "DEEPSEEK_API_KEY", "LLM_BASE_URL", "DEEPSEEK_BASE_URL"}:
                parts = shlex.split(value, comments=True)
                if len(parts) != 1:
                    raise ValueError("credential file contains an invalid assignment")
                parsed[key.strip()] = parts[0]
        if not parsed:
            if "\n" in content or not content:
                raise ValueError("credential file must contain a key or supported dotenv assignments")
            parsed["LLM_API_KEY"] = content
        output.update(parsed)
        key = parsed.get("DEEPSEEK_API_KEY") or parsed.get("LLM_API_KEY")
    else:
        key = output.get("DEEPSEEK_API_KEY") or output.get("LLM_API_KEY")
    if key:
        output.update(DEEPSEEK_API_KEY=key, LLM_API_KEY=key)
    return output


@contextlib.contextmanager
def _model_environment(env: dict[str, str]):
    names = ("DEEPSEEK_API_KEY", "LLM_API_KEY", "DEEPSEEK_BASE_URL", "LLM_BASE_URL")
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            if name in env:
                os.environ[name] = env[name]
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _questions(settings: dict[str, Any]) -> list[dict[str, Any]]:
    if not settings.get("bird_data_dir"):
        raise ValueError("--bird-data-dir or BIRD_DATA_DIR is required")
    data = Path(settings["bird_data_dir"])
    questions = evaluator.select_questions(
        evaluator._load_questions(Path(settings.get("dev_json") or data / "dev.json")),
        db_ids=settings.get("db_ids"),
        question_ids=settings.get("question_ids"),
        question_nos=settings.get("question_nos"),
    )
    excluded = set()
    if settings["exclude_known_questions"]:
        excluded |= read_question_ids_file(Path(__file__).with_name("resources") / "question_id.md")
    if settings.get("exclude_question_ids"):
        excluded |= read_question_ids_file(Path(settings["exclude_question_ids"]))
    questions = [q for q in questions if int(q["question_id"]) not in excluded]
    if not questions:
        raise ValueError("no questions selected")
    if len({case_key(q) for q in questions}) != len(questions):
        raise ValueError("duplicate selected case")
    return questions


def _assets(
    command: str, settings: dict[str, Any], env: dict[str, str], questions: list[dict[str, Any]] | None = None
) -> None:
    from dataagent.core.suite.builtin_suites.bird_benchmark.assets import import_assets, prepare_assets, verify_assets

    if not settings.get("preprocess_root"):
        raise ValueError("--preprocess-root is required")
    root = Path(settings["preprocess_root"])
    db_ids = sorted({str(q["db_id"]) for q in questions}) if questions else settings.get("db_ids")
    common = {"semantic_db_prefix": settings["semantic_db_prefix"], "db_ids": db_ids}
    if command == "verify":
        verify_assets(root, **common)
        return
    url = settings.get("semantic_service_url")
    if not url:
        raise ValueError("--semantic-service-url is required")
    if command == "import":
        import_assets(root, semantic_service_url=url, **common)
        return
    for field in ("bird_data_dir", "train_cache", "preprocess_model", "preprocess_api_base"):
        if not settings.get(field):
            raise ValueError(f"--{field.replace('_', '-')} is required for prepare")
    model = {key: settings.get("preprocess_" + key) for key in REQUEST_FIELDS}
    limits = {
        key: settings[key]
        for key in (
            "value_mode",
            "text_distinct_max_cardinality",
            "max_values_per_column",
            "max_values_per_db",
            "max_yaml_bytes",
        )
    }
    with _model_environment(env):
        prepare_assets(
            bird_data_dir=Path(settings["bird_data_dir"]),
            train_cache=Path(settings["train_cache"]),
            output_root=root,
            semantic_service_url=url,
            **common,
            **model,
            **limits,
        )


def _round_layout(
    root: Path, questions: list[dict[str, Any]], settings: dict[str, Any]
) -> tuple[list[int | None], list[list[list[Any]]]]:
    count = min(settings["workers"], len(questions))
    budgets = worker_budgets(count, settings["llm_max_concurrency"])
    manifest_path = root / "workers.json"
    identities = [[list(case_key(q)) for q in questions[i::count]] for i in range(count)]
    if manifest_path.exists() and _read(manifest_path)["cases"] != identities:
        raise ValueError("resume requires the original case-to-worker layout; use a new run directory")
    return budgets, identities


def _round(
    root: Path, questions: list[dict[str, Any]], settings: dict[str, Any], env: dict[str, str], *, resume: bool
) -> list[dict[str, Any]]:
    budgets, identities = _round_layout(root, questions, settings)
    count = len(budgets)
    _write(root / "workers.json", {"cases": identities, "budgets": budgets})
    processes: list[subprocess.Popen] = []
    streams = []
    try:
        for index, budget in enumerate(budgets):
            config = {**settings, "llm_max_concurrency": budget, "worker_index": index}
            config_path = root / f"worker_{index}.json"
            question_path = root / f"questions_{index}.json"
            _write(config_path, config)
            _write(question_path, questions[index::count])
            stream = (root / f"shard_{index}.log").open("a", encoding="utf-8")
            streams.append(stream)
            command = [
                sys.executable,
                "-m",
                MODULE,
                "_worker",
                "--settings",
                str(config_path),
                "--questions",
                str(question_path),
                "--run-dir",
                str(root / f"shard_{index}"),
            ]
            if resume:
                command.append("--resume")
            processes.append(subprocess.Popen(command, env=env, stdout=stream, stderr=subprocess.STDOUT))
        print(f"BIRD round: {root}; workers={count}; LLM budgets={budgets}; selected={len(questions)}", flush=True)
        pending = set(processes)
        while pending:
            for process in list(pending):
                code = process.poll()
                if code is None:
                    continue
                pending.remove(process)
                if code:
                    raise RuntimeError(f"worker failed with exit {code}; see {root}/shard_*.log; resume to continue")
            if pending:
                time.sleep(0.2)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for stream in streams:
            stream.close()
    rows = load_results(root)
    if {case_key(row) for row in rows} != {case_key(question) for question in questions}:
        raise ValueError("worker results do not cover exactly the selected cases")
    return rows


def _retry(
    root: Path,
    initial: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    settings: dict[str, Any],
    env: dict[str, str],
) -> list[dict[str, Any]]:
    selected = retry_cases(initial, categories=tuple(settings["retry_on"].split(",")))
    allowed = {case_key(question) for question in questions}
    keys = {case_key(row) for row in selected} & allowed
    todo = [question for question in questions if case_key(question) in keys]
    if len(todo) != len(keys):
        raise ValueError("retry inputs are missing original question records")
    retry_root = root / "retry_once"
    if not todo:
        _write(root / "effective_summary.json", summarize_results(initial))
        return initial
    retry_settings = {
        **settings,
        "case_timeout": settings["retry_case_timeout"],
        "generator_max_tokens": settings["retry_generator_max_tokens"],
    }
    _round_layout(retry_root, todo, retry_settings)
    _write(
        retry_root / "plan.json",
        {
            "cases": todo,
            "case_timeout": retry_settings["case_timeout"],
            "generator_max_tokens": retry_settings["generator_max_tokens"],
        },
    )
    rows = _round(retry_root, todo, retry_settings, env, resume=bool(settings.get("resume")))
    effective = merge_retry_results(initial, rows)
    _write(retry_root / "retry_summary.json", summarize_results(rows))
    _write(root / "effective_summary.json", summarize_results(effective))
    return effective


def _initial_results(root: Path) -> list[dict[str, Any]]:
    if (root / "full").is_dir():
        return load_results(root / "full")
    if (root / "summary_initial.json").is_file():
        return load_results(root / "summary_initial.json")
    return load_results(root)


def _effective_results(root: Path, initial: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retry_root = root / "retry_once"
    if not retry_root.is_dir():
        return initial
    plan = _read(retry_root / "plan.json") if (retry_root / "plan.json").is_file() else {}
    if plan.get("cases") == []:
        return initial
    rows = load_results(retry_root)
    if "cases" in plan and {case_key(row) for row in rows} != {case_key(case) for case in plan["cases"]}:
        raise ValueError("retry round is incomplete or does not match its selected cases")
    return merge_retry_results(initial, rows)


def _worker(args: argparse.Namespace) -> None:
    settings = _read(Path(args.settings))
    kwargs = {key: settings.get(key) for key in REQUEST_FIELDS if key != "model"}
    asyncio.run(
        evaluator.run_evaluation(
            bird_data_dir=Path(settings["bird_data_dir"]),
            questions=_read(Path(args.questions)),
            run_dir=Path(args.run_dir),
            resume=args.resume,
            semantic_service_url=settings["semantic_service_url"],
            semantic_db_prefix=settings["semantic_db_prefix"],
            model_name=settings.get("model"),
            case_timeout=settings["case_timeout"],
            generator_max_tokens=settings["generator_max_tokens"],
            llm_max_concurrency=settings["llm_max_concurrency"],
            **kwargs,
        )
    )


def _check_llm(settings: dict[str, Any], env: dict[str, str]) -> None:
    from dataagent.core.managers.llm_manager.llm_client import LLMClient

    if not settings.get("api_base") or not settings.get("model"):
        raise ValueError("check requires --api-base and --model (or corresponding environment variables)")
    key = env.get("DEEPSEEK_API_KEY") or env.get("LLM_API_KEY")
    if not key:
        raise ValueError("check requires a credential environment variable or --api-key-file")
    body = normalize_extra_body(
        thinking=settings["thinking"],
        enable_thinking=settings["enable_thinking"],
        reasoning_effort=settings["reasoning_effort"],
        extra_body=settings["extra_body"],
    )
    client = LLMClient(
        model=settings["model"],
        api_base=settings["api_base"],
        api_key=key,
        timeout=settings["llm_timeout"],
        num_retries=0,
        extra_body=body,
    )
    try:
        asyncio.run(client.ainvoke([{"role": "user", "content": "Reply with OK."}], max_tokens=8))
    except Exception as exc:
        raise ValueError(f"LLM check failed: {str(exc).replace(key, '***')}") from None
    print(f"LLM check passed: model={settings['model']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "prepare", "verify", "import", "check", "retry", "aggregate"):
        item = commands.add_parser(command, argument_default=argparse.SUPPRESS)
        item.add_argument("--mode", choices=tuple(PRESETS))
        for name in (
            "bird-data-dir",
            "dev-json",
            "run-dir",
            "train-cache",
            "preprocess-root",
            "semantic-service-url",
            "semantic-db-prefix",
            "workers",
            "llm-max-concurrency",
            "generator-max-tokens",
            "case-timeout",
            "retry-case-timeout",
            "retry-generator-max-tokens",
            "retry-on",
            "exclude-question-ids",
            "value-mode",
            "text-distinct-max-cardinality",
            "max-values-per-column",
            "max-values-per-db",
            "max-yaml-bytes",
        ):
            item.add_argument("--" + name)
        for prefix in ("", "preprocess-"):
            for name in (
                "model",
                "api-base",
                "api-key-file",
                "thinking",
                "reasoning-effort",
                "extra-body",
                "llm-timeout",
                "llm-num-retries",
            ):
                item.add_argument("--" + prefix + name)
            item.add_argument("--" + prefix + "enable-thinking", nargs="?", const="true")
        item.add_argument("--preprocess", choices=("skip", "reuse", "prepare"))
        item.add_argument("--retry", action=argparse.BooleanOptionalAction)
        item.add_argument("--exclude-known-questions", action=argparse.BooleanOptionalAction)
        item.add_argument("--resume", action="store_true")
        item.add_argument("--dry-run", action="store_true")
        item.add_argument("--db-id", action="append", dest="db_ids")
        item.add_argument("--question-id", action="append", dest="question_ids")
        item.add_argument("--question-no", action="append", type=int, dest="question_nos")
    worker = commands.add_parser("_worker", help=argparse.SUPPRESS)
    for name in ("settings", "questions", "run-dir"):
        worker.add_argument("--" + name, required=True)
    worker.add_argument("--resume", action="store_true")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command == "_worker":
        _worker(args)
        return
    values = vars(args)
    try:
        env = _credentials(values.get("api_key_file"), dict(os.environ))
        saved = {}
        root = Path(values["run_dir"]).resolve() if values.get("run_dir") else None
        if (
            root
            and (values.get("resume") or args.command in {"retry", "aggregate"})
            and (root / "resolved_config.json").is_file()
        ):
            saved = _read(root / "resolved_config.json")
        settings = resolve_options(values, env, saved)
        if args.command == "check":
            if settings.get("dry_run"):
                print(json.dumps(settings, ensure_ascii=False, indent=2))
            else:
                _check_llm(settings, env)
            return
        if args.command in {"prepare", "verify", "import"}:
            if settings.get("dry_run"):
                print(json.dumps(settings, ensure_ascii=False, indent=2))
                return
            pre_env = _credentials(values.get("preprocess_api_key_file"), env)
            _assets(args.command, settings, pre_env)
            return
        if args.command == "aggregate":
            if root is None:
                raise ValueError("--run-dir is required")
            initial = _initial_results(root)
            effective = _effective_results(root, initial)
            if not settings.get("dry_run"):
                _write(root / "summary_initial.json", summarize_results(initial))
                _write(root / "effective_summary.json", summarize_results(effective))
            print(json.dumps({k: v for k, v in summarize_results(effective).items() if k != "results"}))
            return
        questions = _questions(settings)
        if settings.get("dry_run"):
            print(
                json.dumps(
                    {
                        **settings,
                        "selected": len(questions),
                        "worker_budgets": worker_budgets(settings["workers"], settings["llm_max_concurrency"]),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if not settings.get("semantic_service_url"):
            raise ValueError("--semantic-service-url is required")
        if args.command == "retry":
            if root is None:
                raise ValueError("--run-dir is required")
            initial = _initial_results(root)
            selected_count = len(initial)
            initial_keys = {case_key(row) for row in initial}
            questions = [question for question in questions if case_key(question) in initial_keys]
        else:
            root = root or dataagent_home() / "bird_e2e_runs" / (
                datetime.now().strftime("run_%Y%m%d_%H%M%S_") + str(os.getpid())
            )
            if root.exists() and not settings.get("resume"):
                raise FileExistsError(f"run directory exists: {root}; use --resume or another directory")
            _round_layout(root / "full", questions, settings)
            if settings["preprocess"] != "skip":
                _assets(
                    "verify" if settings["preprocess"] == "reuse" else "prepare",
                    settings,
                    _credentials(values.get("preprocess_api_key_file"), env),
                    questions,
                )
            _write(root / "resolved_config.json", settings)
            _write(root / "selected_questions.json", questions)
            initial = _round(root / "full", questions, settings, env, resume=bool(settings.get("resume")))
            selected_count = len(questions)
        _write(root / "summary_initial.json", summarize_results(initial, selected=selected_count))
        effective = (
            _retry(root, initial, questions, settings, env) if settings["retry"] or args.command == "retry" else initial
        )
        _write(root / "summary.json", summarize_results(effective, selected=selected_count))
        _write(root / "effective_summary.json", summarize_results(effective, selected=selected_count))
        summary = summarize_results(effective, selected=selected_count)
        print(
            f"BIRD artifacts: {root}; completed={summary['completed']}/{summary['selected']} correct={summary['correct']} accuracy={summary['accuracy']:.4%}"
        )
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
