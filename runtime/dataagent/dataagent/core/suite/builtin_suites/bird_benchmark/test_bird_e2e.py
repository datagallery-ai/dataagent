# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""BIRD end-to-end evaluation against the real semantic service.

The harness intentionally keeps every selected case running before it raises a
single aggregate assertion. Each case receives its own user, session and
workspace. SQLite snapshots are shared by hard link within a run so a full-dev
evaluation does not duplicate hundreds of gigabytes of immutable database data. Runtime config,
logs, context dumps, messages, SQL files, final state and statistics are kept
under the run directory for post-mortem analysis.

Examples::

    uv run python -m dataagent.core.suite.builtin_suites.bird_benchmark.test_bird_e2e \
      --bird-data-dir /path/to/bird/dev --db-id california_schools \
      --question-id 0 --semantic-service-url http://semantic-service:32000
    uv run python -m dataagent.core.suite.builtin_suites.bird_benchmark.test_bird_e2e \
      --bird-data-dir /path/to/bird/dev --question-no 1 \
      --semantic-service-url http://semantic-service:32000
    uv run python -m dataagent.core.suite.builtin_suites.bird_benchmark.test_bird_e2e \
      --bird-data-dir /path/to/bird/dev --semantic-service-url http://semantic-service:32000

This is the original standalone evaluator relocated into the installed package.
Run artifacts default to the writable DataAgent home directory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import time
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from dataagent.interface.sdk.agent import DataAgent
from dataagent.utils.runtime_paths import dataagent_home, dataagent_package_root

from .model_options import REQUEST_CONTROL_KEYS, normalize_api_base, normalize_extra_body

PACKAGE_DIR = dataagent_package_root()
SUITE_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG = PACKAGE_DIR / "agents" / "bird" / "bird_agent.yaml"
PROVISIONAL_FEW_SHOT_PROFILE = {
    "status": "provisional",
    "retrieval_enabled": True,
    "top_k": 3,
    "client_post_processing": "none",
}
BIRD_DB_IDS = (
    "california_schools",
    "card_games",
    "codebase_community",
    "debit_card_specializing",
    "european_football_2",
    "financial",
    "formula_1",
    "student_club",
    "superhero",
    "thrombosis_prediction",
    "toxicology",
)


class CaseDeadlineExceeded(TimeoutError):
    """A complete Agent chat exceeded the configured evaluation deadline."""


async def _chat_with_deadline(coro: Any, timeout: float | None) -> Any:
    """Await one whole case and synchronously drain cancellation on timeout."""
    task = asyncio.create_task(coro)
    try:
        if timeout is None:
            return await task
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except TimeoutError as exc:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise CaseDeadlineExceeded(f"exceeded {timeout:g} seconds") from exc
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _digest_paths(paths: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for label, path in sorted(paths):
        digest.update(label.encode())
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _semantic_artifact_digest(root: Path, train_cache: Path) -> str:
    paths = [(str(path.relative_to(root)), path) for path in (root / "descriptions").glob("*.json")]
    paths += [(str(path.relative_to(root)), path) for path in (root / "osi").glob("*.yaml")]
    paths.append((train_cache.name, train_cache))
    return _digest_paths(paths)


def _bird_data_digest(bird_data_dir: Path) -> str:
    paths = [("dev_tables.json", bird_data_dir / "dev_tables.json")]
    for db_id in BIRD_DB_IDS:
        sqlite_path = _resolve_sqlite(bird_data_dir, db_id)
        paths.append((str(sqlite_path.relative_to(bird_data_dir)), sqlite_path))
        description_dir = bird_data_dir / "dev_databases" / db_id / "database_description"
        paths.extend((str(path.relative_to(bird_data_dir)), path) for path in sorted(description_dir.glob("*.csv")))
    return _digest_paths(paths)


def _semantic_preprocess_input_digest(bird_data_dir: Path, train_cache: Path) -> str:
    paths = [
        ("train_cache.json", train_cache),
        ("generator/generate_llm_column_desc.py", Path(__file__).with_name("generate_llm_column_desc.py")),
        ("generator/bird_to_osi_yaml.py", Path(__file__).with_name("bird_to_osi_yaml.py")),
    ]
    paths.extend(
        (f"bird/{label}", path)
        for label, path in [
            ("dev_tables.json", bird_data_dir / "dev_tables.json"),
            *[
                (str(path.relative_to(bird_data_dir)), path)
                for db_id in BIRD_DB_IDS
                for path in (
                    _resolve_sqlite(bird_data_dir, db_id),
                    *sorted((bird_data_dir / "dev_databases" / db_id / "database_description").glob("*.csv")),
                )
            ],
        ]
    )
    return _digest_paths(paths)


def _validate_semantic_preprocess_artifacts(
    *,
    root: Path,
    prefix: str,
    value_mode: str,
    text_distinct_max_cardinality: int,
    max_values_per_column: int,
    max_values_per_db: int,
    max_yaml_bytes: int,
) -> tuple[str, str]:
    expected = sorted(BIRD_DB_IDS)
    descriptions = sorted((root / "descriptions").glob("*.json"))
    yaml_files = sorted((root / "osi").glob("*.yaml"))
    stats_files = sorted((root / "logs").glob("*_value_stats.json"))
    validation_files = sorted((root / "logs").glob("*_import_validation.json"))
    import_files = sorted((root / "import_responses").glob("*.json"))
    observed = {
        "descriptions": [path.stem for path in descriptions],
        "osi": [path.stem for path in yaml_files],
        "stats": [path.name.removesuffix("_value_stats.json") for path in stats_files],
        "import validations": [path.name.removesuffix("_import_validation.json") for path in validation_files],
        "import responses": [path.stem for path in import_files],
    }
    for kind, names in observed.items():
        if names != expected:
            raise ValueError(f"semantic {kind} do not match the 11 canonical BIRD databases")

    limits = {
        "max_values_per_column": max_values_per_column,
        "max_values_per_db": max_values_per_db,
        "max_yaml_bytes": max_yaml_bytes,
        "text_distinct_max_cardinality": text_distinct_max_cardinality,
    }
    for yaml_path in yaml_files:
        db_id = yaml_path.stem
        service_db_id = semantic_db_id(db_id, prefix)
        document = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        models = document.get("semantic_model", []) if isinstance(document, dict) else []
        if len(models) != 1 or models[0].get("name") != f"{service_db_id}_semantic_model":
            raise ValueError(f"semantic model namespace mismatch in {yaml_path.name}")
        model = models[0]
        for dataset in model.get("datasets", []):
            if not all(str(dataset.get(key, "")).startswith(f"{service_db_id}.") for key in ("name", "source")):
                raise ValueError(f"semantic dataset namespace mismatch in {yaml_path.name}")
            extension = dataset.get("custom_extensions", {})
            if any(extension.get(key) != service_db_id for key in ("schema_name", "db_name_en")):
                raise ValueError(f"semantic dataset extension mismatch in {yaml_path.name}")
            if any(
                field.get("custom_extensions", {}).get("db_name_en") != service_db_id
                for field in dataset.get("fields", [])
            ):
                raise ValueError(f"semantic field namespace mismatch in {yaml_path.name}")
        extensions = model.get("custom_extensions", [])
        graph = extensions[0].get("data", {}).get("graph", {}) if extensions else {}
        nodes = graph.get("nodes", {})
        values = nodes.get("column_values", [])
        if any(
            not str(value.get("physical_ref", {}).get("dataset", "")).startswith(f"{service_db_id}.")
            for value in values
        ):
            raise ValueError(f"semantic column-value namespace mismatch in {yaml_path.name}")
        if any(
            not str(process.get("qualified_name", "")).startswith(f"{service_db_id}.")
            or process.get("source_tables") != [service_db_id]
            for process in nodes.get("sql_processes", [])
        ):
            raise ValueError(f"semantic SQL-process namespace mismatch in {yaml_path.name}")

        stats = json.loads((root / "logs" / f"{db_id}_value_stats.json").read_text(encoding="utf-8"))
        if stats.get("db_id") != db_id or stats.get("semantic_db_id") != service_db_id:
            raise ValueError(f"semantic value stats namespace mismatch for {db_id}")
        if stats.get("value_mode") != value_mode or stats.get("limits") != limits:
            raise ValueError(f"semantic value policy mismatch for {db_id}")
        totals = stats.get("totals", {})
        if totals.get("selected") != len(values) or totals.get("selected", max_values_per_db + 1) > max_values_per_db:
            raise ValueError(f"semantic value stats count mismatch for {db_id}")
        if totals.get("yaml_bytes", max_yaml_bytes + 1) > max_yaml_bytes:
            raise ValueError(f"semantic YAML cap mismatch for {db_id}")
        validation = json.loads((root / "logs" / f"{db_id}_import_validation.json").read_text(encoding="utf-8"))
        if validation.get("emitted") != len(values):
            raise ValueError(f"semantic import validation count mismatch for {db_id}")
        for family in ("data_column_value_desc_filled", "data_column_value_val_filled"):
            if validation.get(family) != len(values):
                raise ValueError(f"semantic vector-fill validation mismatch for {db_id}: {family}")

    stats_digest = _digest_paths([(path.name, path) for path in stats_files])
    import_validation_digest = _digest_paths([(path.name, path) for path in validation_files])
    return stats_digest, import_validation_digest


def semantic_preprocess_provenance(
    mode: str,
    *,
    root: Path,
    train_cache: Path,
    bird_data_dir: Path,
    model: str,
    prefix: str,
    value_mode: str,
    text_distinct_max_cardinality: int,
    max_values_per_column: int,
    max_values_per_db: int,
    max_yaml_bytes: int,
) -> None:
    stats_digest, import_validation_digest = _validate_semantic_preprocess_artifacts(
        root=root,
        prefix=prefix,
        value_mode=value_mode,
        text_distinct_max_cardinality=text_distinct_max_cardinality,
        max_values_per_column=max_values_per_column,
        max_values_per_db=max_values_per_db,
        max_yaml_bytes=max_yaml_bytes,
    )
    # Fingerprints describe an import; they do not gate reuse of valid artifacts.
    if mode != "write":
        return
    artifact_digest = _semantic_artifact_digest(root, train_cache)
    expected = {
        "version": 2,
        "semantic_preprocess_model": model,
        "semantic_db_prefix": prefix,
        "semantic_value_mode": value_mode,
        "semantic_text_distinct_max_cardinality": text_distinct_max_cardinality,
        "semantic_max_values_per_column": max_values_per_column,
        "semantic_max_values_per_db": max_values_per_db,
        "semantic_max_yaml_bytes": max_yaml_bytes,
        "db_ids": list(BIRD_DB_IDS),
        "semantic_model_digest": artifact_digest,
        "bird_data_digest": _bird_data_digest(bird_data_dir),
        "semantic_preprocess_input_digest": _semantic_preprocess_input_digest(bird_data_dir, train_cache),
        "value_stats_digest": stats_digest,
        "import_validation_digest": import_validation_digest,
    }
    digest_path = root / "semantic_model.sha256"
    provenance_path = root / "semantic_preprocess_provenance.json"
    if mode == "write":
        digest_path.write_text(f"{artifact_digest}\n", encoding="utf-8")
        _write_json(provenance_path, expected)
        return


def _tree_digest(root: Path) -> str:
    paths = sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix in {".json", ".md", ".py", ".sh", ".yaml", ".yml"}
    )
    if not paths:
        raise FileNotFoundError(f"No files available for digest under {root}")
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _source_tree_digest() -> str:
    roots = (
        PACKAGE_DIR / "agents" / "bird",
        PACKAGE_DIR / "actions" / "tools" / "semantic_tool",
        PACKAGE_DIR / "core" / "managers" / "llm_manager",
        SUITE_DIR,
    )
    paths = [
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".md", ".py", ".sh", ".yaml", ".yml"}
    ]
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(str(path.relative_to(PACKAGE_DIR)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _candidate_sha() -> str:
    """Record caller-supplied source identity without requiring a Git checkout."""
    return os.getenv("DATAAGENT_CANDIDATE_SHA") or "unversioned"


def _evaluation_manifest(
    *,
    evaluation_fingerprint: str,
    semantic_service_url: str,
    semantic_preprocess_model: str,
    semantic_model_digest: str,
    semantic_db_prefix: str,
    effective_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bird_agent_root = PACKAGE_DIR / "agents" / "bird"
    return {
        "evaluation_fingerprint": evaluation_fingerprint,
        "candidate_sha": _candidate_sha(),
        "agent_type": "bird",
        "bird_agent_root": str(bird_agent_root),
        "bird_agent_tree_digest": _tree_digest(bird_agent_root),
        "source_tree_digest": _source_tree_digest(),
        "few_shot_profile": PROVISIONAL_FEW_SHOT_PROFILE,
        "prompt_digest": _tree_digest(bird_agent_root / "prompts"),
        "base_config_digest": hashlib.sha256(DEFAULT_CONFIG.read_bytes()).hexdigest(),
        "effective_config": effective_config,
        "semantic_binding": {
            "service_url": semantic_service_url.rstrip("/"),
            "preprocess_model": semantic_preprocess_model,
            "model_digest": semantic_model_digest,
            "db_prefix": semantic_db_prefix,
        },
    }


def _evaluation_fingerprint(
    *,
    bird_data_dir: Path,
    questions: list[dict[str, Any]],
    semantic_service_url: str,
    model_name: str | None,
    enable_thinking: bool | str,
    llm_timeout: float | None,
    llm_num_retries: int | None,
    case_timeout: float | None,
    semantic_preprocess_model: str,
    semantic_model_digest: str,
    semantic_db_prefix: str = "bird",
    effective_config: dict[str, Any] | None = None,
) -> str:
    """Identify the code, prompts, base config, and runtime overrides used by a run."""
    digest = hashlib.sha256()
    bird_agent_root = PACKAGE_DIR / "agents" / "bird"
    runtime = {
        "candidate_sha": _candidate_sha(),
        "agent_type": "bird",
        "bird_agent_root": str(bird_agent_root),
        "bird_agent_tree_digest": _tree_digest(bird_agent_root),
        "source_tree_digest": _source_tree_digest(),
        "few_shot_profile": PROVISIONAL_FEW_SHOT_PROFILE,
        "prompt_digest": _tree_digest(bird_agent_root / "prompts"),
        "base_config_digest": hashlib.sha256(DEFAULT_CONFIG.read_bytes()).hexdigest(),
        "semantic_service_url": semantic_service_url.rstrip("/"),
        "model_name": model_name,
        "enable_thinking": enable_thinking,
        "llm_timeout": llm_timeout,
        "llm_num_retries": llm_num_retries,
        # Normalize integer/float spellings so equivalent deadlines resume,
        # while disabled and genuinely different deadlines never collide.
        "case_timeout": None if case_timeout is None else float(case_timeout),
        "semantic_preprocess_model": semantic_preprocess_model,
        "llm_endpoint": os.getenv("DEEPSEEK_BASE_URL") or os.getenv("LLM_BASE_URL"),
        "semantic_model_digest": semantic_model_digest,
        "semantic_db_prefix": semantic_db_prefix,
        "effective_config": effective_config,
    }
    digest.update(json.dumps(runtime, sort_keys=True).encode())
    digest.update(json.dumps(questions, ensure_ascii=False, sort_keys=True).encode())
    inputs: list[Path] = [
        Path(__file__),
        DEFAULT_CONFIG,
        bird_data_dir / "dev_tables.json",
        PACKAGE_DIR / "actions/tools/semantic_tool/semantic_client.py",
    ]
    for root in (
        PACKAGE_DIR / "agents/bird",
        PACKAGE_DIR / "core/managers/llm_manager",
    ):
        inputs.extend(
            path for path in root.rglob("*") if path.is_file() and path.suffix in {".py", ".md", ".yaml", ".yml"}
        )
    for path in sorted(set(inputs)):
        try:
            label = path.relative_to(PACKAGE_DIR)
        except ValueError:
            label = path.resolve()
        digest.update(str(label).encode())
        digest.update(path.read_bytes())
    for db_id in sorted({str(item["db_id"]) for item in questions}):
        db_path = _resolve_sqlite(bird_data_dir, db_id)
        stat = db_path.stat()
        digest.update(str(db_path.relative_to(bird_data_dir)).encode())
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def _load_questions(dev_json: Path) -> list[dict[str, Any]]:
    payload = json.loads(dev_json.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON list in {dev_json}")
    required = {"question_id", "db_id", "question", "SQL"}
    for index, item in enumerate(payload):
        missing = required - set(item)
        if missing:
            raise ValueError(f"Question {index} is missing fields: {sorted(missing)}")
    return payload


def select_questions(
    questions: list[dict[str, Any]],
    *,
    db_ids: list[str] | None = None,
    question_ids: list[str] | None = None,
    question_nos: list[int] | None = None,
) -> list[dict[str, Any]]:
    selected = questions
    if db_ids:
        wanted_dbs = set(db_ids)
        selected = [item for item in selected if str(item["db_id"]) in wanted_dbs]
    if question_ids:
        wanted_ids = set(question_ids)
        selected = [item for item in selected if str(item["question_id"]) in wanted_ids]
    if question_nos:
        bad = [number for number in question_nos if number < 1 or number > len(selected)]
        if bad:
            raise ValueError(f"question-no out of range 1..{len(selected)}: {bad}")
        selected = [selected[number - 1] for number in question_nos]
    if not selected:
        raise ValueError("No BIRD questions matched the requested selectors")
    return selected


def semantic_db_id(db_id: str, prefix: str = "bird") -> str:
    """Return the isolated semantic-service namespace selected by task 10.3."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", prefix):
        raise ValueError(f"Invalid semantic DB prefix: {prefix!r}")
    return f"{prefix}_{db_id}"


def _resolve_sqlite(bird_data_dir: Path, db_id: str) -> Path:
    candidates = (
        bird_data_dir / "dev_databases" / db_id / f"{db_id}.sqlite",
        bird_data_dir / "dev_databases" / f"{db_id}.sqlite",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"BIRD SQLite not found for {db_id}: {candidates[0]}")


def _shared_sqlite_snapshot(run_root: Path, source_db: Path, db_id: str) -> Path:
    """Keep one immutable SQLite snapshot per database in a run directory."""
    snapshot_dir = run_root / "_databases"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_dir / f"{db_id}.sqlite"
    if snapshot.exists():
        if snapshot.stat().st_size != source_db.stat().st_size:
            raise ValueError(f"Existing database snapshot has wrong size: {snapshot}")
        return snapshot
    temporary = snapshot.with_suffix(".sqlite.copying")
    shutil.copy2(source_db, temporary)
    temporary.replace(snapshot)
    return snapshot


def _link_sqlite_snapshot(snapshot: Path, destination: Path) -> None:
    """Expose the run snapshot in a case workspace without duplicating bytes."""
    if destination.exists():
        return
    try:
        destination.hardlink_to(snapshot)
    except OSError:
        try:
            destination.symlink_to(snapshot)
        except OSError:
            shutil.copy2(snapshot, destination)


def _positive_number(value: Any, name: str, *, integer: bool = False) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive {'integer' if integer else 'number'} or None")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} must be a positive integer or None")
    return value


def _apply_runtime_options(
    config: dict[str, Any],
    *,
    model_name: str | None,
    api_base: str | None = None,
    thinking: str = "omit",
    enable_thinking: bool | str = "omit",
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
    llm_timeout: float | None = None,
    llm_num_retries: int | None = None,
    generator_max_tokens: int | None = None,
    llm_max_concurrency: int | None = None,
    case_timeout: float | None = None,
) -> dict[str, Any]:
    """Resolve the request once without persisting provider credentials."""
    _positive_number(llm_timeout, "llm_timeout")
    _positive_number(case_timeout, "case_timeout")
    _positive_number(generator_max_tokens, "generator_max_tokens", integer=True)
    _positive_number(llm_max_concurrency, "llm_max_concurrency", integer=True)
    if llm_num_retries is not None and (
        isinstance(llm_num_retries, bool) or not isinstance(llm_num_retries, int) or llm_num_retries < 0
    ):
        raise ValueError("llm_num_retries must be a nonnegative integer or None")
    normalized_body = normalize_extra_body(
        thinking=thinking,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
    )
    model_sections = config.get("MODEL") or {}
    if len(model_sections) != 1:
        raise ValueError("BIRD evaluation requires exactly one MODEL section in the base config")
    model_key, model_config = next(iter(model_sections.items()))
    model_params = model_config.setdefault("params", {})
    model_config.pop("api_key", None)
    model_params.pop("api_key", None)
    inherited_body = dict(model_params.pop("extra_body", None) or {})
    for key in REQUEST_CONTROL_KEYS:
        model_params.pop(key, None)
        inherited_body.pop(key, None)
    # Validate base extensions too: request identity and transport settings must
    # not be shadowed by values merged into the JSON body by the shared client.
    inherited_body = normalize_extra_body(extra_body=inherited_body)
    inherited_body.update(normalized_body)
    for key in normalized_body:
        model_params.pop(key, None)
    if inherited_body:
        model_params["extra_body"] = inherited_body
    if model_name is not None:
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a nonempty string")
        model_params["model"] = model_name.strip()
    provider_env = str(model_config.get("provider") or model_key).upper().replace("-", "_")
    endpoint = (
        api_base
        or model_params.get("base_url")
        or model_params.get("api_base")
        or os.getenv(f"{provider_env}_BASE_URL")
        or os.getenv("LLM_BASE_URL")
    )
    model_params.pop("api_base", None)
    if endpoint:
        model_params["base_url"] = normalize_api_base(endpoint)
    if llm_timeout is not None:
        model_params["timeout"] = llm_timeout
    if llm_num_retries is not None:
        model_params["num_retries"] = llm_num_retries
    _positive_number(model_params.get("timeout"), "configured llm_timeout")
    configured_retries = model_params.get("num_retries")
    if configured_retries is not None and (
        isinstance(configured_retries, bool) or not isinstance(configured_retries, int) or configured_retries < 0
    ):
        raise ValueError("configured llm_num_retries must be a nonnegative integer")
    core = config.setdefault("CORE", {})
    core["llm_max_concurrency"] = llm_max_concurrency
    core.setdefault("generator", {})["max_tokens"] = generator_max_tokens
    request_body = {
        **inherited_body,
        **{
            key: value
            for key, value in model_params.items()
            if key
            not in {
                "extra_body",
                "model",
                "base_url",
                "timeout",
                "num_retries",
                "custom_llm_provider",
            }
        },
    }
    return {
        "model_name": model_params.get("model"),
        "api_base": model_params.get("base_url"),
        "thinking": request_body.get("thinking", {}).get("type", "omit"),
        "enable_thinking": request_body.get("enable_thinking", "omit"),
        "reasoning_effort": request_body.get("reasoning_effort"),
        "extra_body": request_body,
        "llm_timeout": model_params.get("timeout"),
        "llm_num_retries": model_params.get("num_retries"),
        "case_timeout": case_timeout,
        "generator_max_tokens": generator_max_tokens,
        "llm_max_concurrency": llm_max_concurrency,
    }


def _build_config(
    *,
    output_path: Path,
    sqlite_path: Path,
    service_db_id: str,
    semantic_service_url: str,
    user_id: str,
    session_id: str,
    model_name: str | None,
    api_base: str | None = None,
    thinking: str = "omit",
    enable_thinking: bool | str = "omit",
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
    llm_timeout: float | None = None,
    llm_num_retries: int | None = None,
    generator_max_tokens: int | None = None,
    llm_max_concurrency: int | None = None,
) -> Path:
    config = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    if config.get("AGENT_CONFIG", {}).get("type") != "bird":
        raise ValueError("BIRD evaluation requires AGENT_CONFIG.type: bird")
    if "SUITE" in config:
        raise ValueError("BIRD Agent config must not depend on Suite activation")
    _apply_runtime_options(
        config,
        model_name=model_name,
        api_base=api_base,
        thinking=thinking,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
        llm_timeout=llm_timeout,
        llm_num_retries=llm_num_retries,
        generator_max_tokens=generator_max_tokens,
        llm_max_concurrency=llm_max_concurrency,
    )
    config.setdefault("DATABASE", {})["db_id"] = service_db_id
    config["DATABASE"]["dialect"] = "sqlite"
    config["DATABASE"]["engine"] = None
    config["DATABASE"]["config"] = {"path": str(sqlite_path)}
    semantic = config.setdefault("SEMANTIC_LAYER", {})
    semantic["base_url"] = semantic_service_url.rstrip("/")
    semantic["timeout"] = max(int(semantic.get("timeout") or 0), 180)
    config["USER_ID"] = user_id
    config["SESSION_ID"] = session_id
    output_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return output_path


def _execute_sql(db_path: Path, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    if not sql.strip():
        raise ValueError("SQL is empty")
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        cursor = connection.execute(sql)
        columns = [item[0] for item in (cursor.description or [])]
        rows = cursor.fetchall()
    return columns, rows


def _copy_session_log(case_dir: Path, user_id: str, session_id: str) -> Path | None:
    source = dataagent_home() / user_id / "logs" / f"main_{session_id}.log"
    if not source.is_file():
        return None
    destination = case_dir / "session.log"
    shutil.copy2(source, destination)
    return destination


def _result_set(rows: list[tuple[Any, ...]]) -> set[tuple[Any, ...]]:
    return {tuple(row) for row in rows}


async def _run_case(
    item: dict[str, Any],
    *,
    bird_data_dir: Path,
    run_root: Path,
    semantic_service_url: str,
    model_name: str | None,
    enable_thinking: bool | str = "omit",
    llm_timeout: float | None,
    llm_num_retries: int | None,
    case_timeout: float | None,
    evaluation_fingerprint: str,
    semantic_preprocess_model: str,
    semantic_model_digest: str,
    semantic_db_prefix: str,
    api_base: str | None = None,
    thinking: str = "omit",
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
    generator_max_tokens: int | None = None,
    llm_max_concurrency: int | None = None,
    effective_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective_config = effective_config or _apply_runtime_options(
        yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8")),
        model_name=model_name,
        api_base=api_base,
        thinking=thinking,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
        llm_timeout=llm_timeout,
        llm_num_retries=llm_num_retries,
        generator_max_tokens=generator_max_tokens,
        llm_max_concurrency=llm_max_concurrency,
        case_timeout=case_timeout,
    )
    db_id = str(item["db_id"])
    question_id = str(item["question_id"])
    case_name = f"{db_id}__q{question_id}"
    case_dir = run_root / case_name
    workspace = case_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    suffix = secrets.token_hex(3)
    user_id = f"bird_e2e_{run_root.name}_{suffix}"
    session_id = f"bird_e2e_{db_id}_{question_id}_{suffix}"
    service_db_id = semantic_db_id(db_id, semantic_db_prefix)
    source_db = _resolve_sqlite(bird_data_dir, db_id)
    snapshot_db = _shared_sqlite_snapshot(run_root, source_db, db_id)
    copied_db = workspace / f"{db_id}.sqlite"
    _link_sqlite_snapshot(snapshot_db, copied_db)

    config_path = _build_config(
        output_path=case_dir / "agent_config.yaml",
        sqlite_path=copied_db,
        service_db_id=service_db_id,
        semantic_service_url=semantic_service_url,
        user_id=user_id,
        session_id=session_id,
        model_name=model_name,
        api_base=api_base,
        thinking=thinking,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
        llm_timeout=llm_timeout,
        llm_num_retries=llm_num_retries,
        generator_max_tokens=generator_max_tokens,
        llm_max_concurrency=llm_max_concurrency,
    )
    (case_dir / "predicted.sql").write_text("", encoding="utf-8")
    (case_dir / "gold.sql").write_text(str(item["SQL"]), encoding="utf-8")
    _write_json(case_dir / "question.json", item)

    log_path = case_dir / "run.log"
    log_sink: int | None = logger.add(log_path, encoding="utf-8")
    started = time.perf_counter()
    final_state: dict[str, Any] = {}
    error: str | None = None
    error_traceback: str | None = None
    deferred = False
    logger.info(
        "BIRD E2E start db_id={} semantic_db_id={} question_id={}",
        db_id,
        service_db_id,
        question_id,
    )
    try:
        agent = DataAgent.from_config(str(config_path))
    except Exception:
        logger.exception("BIRD E2E setup failed")
        if log_sink is not None:
            with contextlib.suppress(ValueError):
                logger.remove(log_sink)
        raise

    try:
        # DataAgent logger initialization can replace pre-existing loguru
        # handlers. Add the case sink again so the complete node run is kept.
        with contextlib.suppress(ValueError):
            logger.remove(log_sink)
        log_sink = logger.add(log_path, encoding="utf-8", mode="a")
        response = await _chat_with_deadline(
            agent.chat(
                str(item["question"]),
                session_id=session_id,
                workspace=workspace,
                initial_state={
                    "user_id": user_id,
                    "session_id": session_id,
                    "run_id": 0,
                    "sub_id": 0,
                    "evidence": str(item.get("evidence") or ""),
                },
            ),
            case_timeout,
        )
        final_state = response if isinstance(response, dict) else {"response": response}
    except CaseDeadlineExceeded as exc:
        deferred = True
        error = f"{type(exc).__name__}: {exc}"
        error_traceback = traceback.format_exc()
        (case_dir / "exception.txt").write_text(error_traceback, encoding="utf-8")
        logger.warning("BIRD E2E case deferred: {}", error)
    except Exception as exc:  # keep later cases running
        error = f"{type(exc).__name__}: {exc}"
        error_traceback = traceback.format_exc()
        (case_dir / "exception.txt").write_text(error_traceback, encoding="utf-8")
        logger.exception("BIRD E2E agent execution failed")
    finally:
        if log_sink is not None:
            with contextlib.suppress(ValueError):
                logger.remove(log_sink)

    elapsed = time.perf_counter() - started
    session_log = _copy_session_log(case_dir, user_id, session_id)
    predicted_sql = str(final_state.get("sql") or "").strip()
    (case_dir / "predicted.sql").write_text(predicted_sql, encoding="utf-8")
    _write_json(case_dir / "messages.json", final_state.get("messages", []))
    _write_json(case_dir / "final_state.json", final_state)

    predicted_rows: list[tuple[Any, ...]] = []
    gold_rows: list[tuple[Any, ...]] = []
    predicted_columns: list[str] = []
    gold_columns: list[str] = []
    if error is None and final_state.get("error"):
        error = f"AgentError: {final_state['error']}"
    try:
        if error is None:
            predicted_columns, predicted_rows = _execute_sql(copied_db, predicted_sql)
    except Exception as exc:
        error = f"PredictedSQL{type(exc).__name__}: {exc}"
    try:
        gold_columns, gold_rows = _execute_sql(copied_db, str(item["SQL"]))
    except Exception as exc:
        error = error or f"GoldSQL{type(exc).__name__}: {exc}"

    correct = error is None and _result_set(predicted_rows) == _result_set(gold_rows)
    result = {
        "question_id": item["question_id"],
        "db_id": db_id,
        "semantic_db_id": service_db_id,
        "question": item["question"],
        "evidence": item.get("evidence") or "",
        "predicted_sql": predicted_sql,
        "gold_sql": item["SQL"],
        "predicted_columns": predicted_columns,
        "gold_columns": gold_columns,
        "predicted_row_count": len(predicted_rows),
        "gold_row_count": len(gold_rows),
        "correct": correct,
        "error": error,
        "error_traceback": error_traceback,
        "deferred": deferred,
        "elapsed_seconds": elapsed,
        "llm_total_tokens": final_state.get("llm_total_tokens", 0),
        "generation_warnings": final_state.get("generation_warnings", []),
        "schema_linking_trace": final_state.get("schema_linking_trace"),
        "artifact_dir": str(case_dir),
        "context_dump_dir": str(workspace / ".memory" / "context_dump"),
        "session_log": str(session_log) if session_log else None,
        "generator_model": model_name,
        "effective_config": effective_config,
        "semantic_preprocess_model": semantic_preprocess_model,
        "semantic_model_digest": semantic_model_digest,
        "evaluation_fingerprint": evaluation_fingerprint,
    }
    _write_json(case_dir / "stats.json", result)
    logger.info("BIRD E2E done {} correct={} elapsed={:.2f}s", case_name, correct, elapsed)
    return result


async def run_evaluation(
    *,
    bird_data_dir: Path,
    questions: list[dict[str, Any]],
    output_root: Path | None = None,
    semantic_service_url: str | None = None,
    model_name: str | None = None,
    api_base: str | None = None,
    thinking: str = "omit",
    enable_thinking: bool | str = "omit",
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
    llm_timeout: float | None = None,
    llm_num_retries: int | None = None,
    case_timeout: float | None = None,
    generator_max_tokens: int | None = None,
    llm_max_concurrency: int | None = None,
    semantic_preprocess_model: str = "DeepSeek-V4-Flash-0731",
    semantic_model_digest: str = "unversioned",
    semantic_db_prefix: str = "bird",
    run_dir: Path | None = None,
    resume: bool = False,
) -> tuple[Path, list[dict[str, Any]]]:
    if not semantic_service_url or not semantic_service_url.strip():
        raise ValueError("semantic_service_url must be explicitly configured")
    os.environ.setdefault("DATAAGENT_CONTEXT_DUMP", "1")
    os.environ.setdefault("DATAAGENT_LOG_LEVEL", "INFO")
    effective_config = _apply_runtime_options(
        yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8")),
        model_name=model_name,
        api_base=api_base,
        thinking=thinking,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
        llm_timeout=llm_timeout,
        llm_num_retries=llm_num_retries,
        generator_max_tokens=generator_max_tokens,
        llm_max_concurrency=llm_max_concurrency,
        case_timeout=case_timeout,
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = output_root or dataagent_home() / "bird_e2e_runs"
    run_root = run_dir or output_root / f"run_{stamp}_{secrets.token_hex(2)}"
    if run_root.exists() and not resume:
        raise FileExistsError(f"Run directory already exists; pass --resume: {run_root}")
    run_root.mkdir(parents=True, exist_ok=resume)
    evaluation_fingerprint = _evaluation_fingerprint(
        bird_data_dir=bird_data_dir,
        questions=questions,
        semantic_service_url=semantic_service_url,
        model_name=model_name,
        enable_thinking=enable_thinking,
        llm_timeout=llm_timeout,
        llm_num_retries=llm_num_retries,
        case_timeout=case_timeout,
        semantic_preprocess_model=semantic_preprocess_model,
        semantic_model_digest=semantic_model_digest,
        semantic_db_prefix=semantic_db_prefix,
        effective_config=effective_config,
    )
    manifest = _evaluation_manifest(
        evaluation_fingerprint=evaluation_fingerprint,
        semantic_service_url=semantic_service_url,
        semantic_preprocess_model=semantic_preprocess_model,
        semantic_model_digest=semantic_model_digest,
        semantic_db_prefix=semantic_db_prefix,
        effective_config=effective_config,
    )
    manifest_path = run_root / "manifest.json"
    if not (resume and manifest_path.is_file()):
        _write_json(manifest_path, manifest)
    else:
        _write_json(run_root / "resume_manifest.json", manifest)
    results: list[dict[str, Any]] = []

    def write_progress() -> dict[str, Any]:
        deferred_results = [result for result in results if result.get("deferred")]
        summary = {
            "selected": len(questions),
            "completed": len(results),
            "predicted": sum(bool(result.get("predicted_sql")) for result in results),
            "correct": sum(bool(result.get("correct")) for result in results),
            "agent_error": sum(bool(result.get("error")) and not result.get("deferred") for result in results),
            "deferred": len(deferred_results),
            "failed": sum(not bool(result.get("correct")) for result in results),
            # Use every selected question as the denominator. A deferred case
            # is never silently removed from the reported benchmark accuracy.
            "accuracy": sum(bool(result.get("correct")) for result in results) / len(questions),
            "semantic_service_url": semantic_service_url,
            "bird_data_dir": str(bird_data_dir),
            "case_timeout_seconds": case_timeout,
            "effective_config": effective_config,
            "results": results,
        }
        _write_json(run_root / "summary.json", summary)
        jsonl = "".join(json.dumps(item, ensure_ascii=False, default=_json_default) + "\n" for item in deferred_results)
        (run_root / "deferred_cases.jsonl").write_text(jsonl, encoding="utf-8")
        with (run_root / "deferred_cases.tsv").open("w", encoding="utf-8", newline="") as stream:
            fields = ("question_id", "db_id", "error", "artifact_dir", "context_dump_dir")
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(deferred_results)
        return summary

    for item in questions:
        case_name = f"{item.get('db_id', 'unknown')}__q{item.get('question_id', 'unknown')}"
        stats_path = run_root / case_name / "stats.json"
        if resume and stats_path.is_file():
            result = json.loads(stats_path.read_text(encoding="utf-8"))
            result.setdefault("deferred", False)
            results.append(result)
            print(f"BIRD E2E resume skip {case_name}", flush=True)
            write_progress()
            continue
        result = await _run_case(
            item,
            bird_data_dir=bird_data_dir,
            run_root=run_root,
            semantic_service_url=semantic_service_url,
            model_name=model_name,
            api_base=api_base,
            thinking=thinking,
            enable_thinking=enable_thinking,
            reasoning_effort=reasoning_effort,
            extra_body=extra_body,
            llm_timeout=llm_timeout,
            llm_num_retries=llm_num_retries,
            case_timeout=case_timeout,
            generator_max_tokens=generator_max_tokens,
            llm_max_concurrency=llm_max_concurrency,
            effective_config=effective_config,
            evaluation_fingerprint=evaluation_fingerprint,
            semantic_preprocess_model=semantic_preprocess_model,
            semantic_model_digest=semantic_model_digest,
            semantic_db_prefix=semantic_db_prefix,
        )
        results.append(result)
        summary = write_progress()
        print(
            f"BIRD E2E progress {len(results)}/{len(questions)} "
            f"correct={summary['correct']} failed={summary['failed']}",
            flush=True,
        )
    return run_root, results


def _assert_all_correct(run_root: Path, results: list[dict[str, Any]]) -> None:
    failures = [item for item in results if not item.get("correct")]
    if failures:
        details = "; ".join(
            f"{item.get('db_id')}/q{item.get('question_id')}: {item.get('error') or 'result mismatch'}"
            for item in failures
        )
        raise AssertionError(f"{len(failures)}/{len(results)} BIRD cases failed: {details}. Artifacts: {run_root}")


def _optional_limit(value: str, *, integer: bool = False) -> int | float | None:
    if value.strip().lower() == "none":
        return None
    try:
        number = int(value) if integer else float(value)
        return _positive_number(number, "limit", integer=integer)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _extra_body_argument(value: str) -> dict[str, Any]:
    try:
        body = json.loads(value)
        if not isinstance(body, dict):
            raise ValueError("extra-body must be a JSON object")
        return normalize_extra_body(extra_body=body)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(
            "extra-body must be a JSON object without controlled or credential fields"
        ) from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bird-data-dir", type=Path, required=True)
    parser.add_argument("--dev-json", type=Path, default=None)
    parser.add_argument("--db-id", action="append", dest="db_ids")
    parser.add_argument("--question-id", action="append", dest="question_ids")
    parser.add_argument("--question-no", action="append", type=int, dest="question_nos")
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--no-assert",
        action="store_true",
        help="Keep benchmark mismatches in artifacts without returning a failing exit code",
    )
    parser.add_argument(
        "--semantic-service-url",
        default=os.getenv("BIRD_SEMANTIC_SERVICE_URL") or os.getenv("SEMANTIC_SERVICE_URL"),
        help="required for evaluation; may also be set via BIRD_SEMANTIC_SERVICE_URL or SEMANTIC_SERVICE_URL",
    )
    parser.add_argument("--semantic-preprocess-provenance", choices=("write", "verify"), default=None)
    parser.add_argument("--semantic-preprocess-root", type=Path, default=None)
    parser.add_argument("--train-cache", type=Path, default=None)
    parser.add_argument(
        "--semantic-value-mode", choices=("sample", "text_distinct", "all_distinct"), default="text_distinct"
    )
    parser.add_argument("--semantic-text-distinct-max-cardinality", type=int, default=1000)
    parser.add_argument("--semantic-max-values-per-column", type=int, default=10000)
    parser.add_argument("--semantic-max-values-per-db", type=int, default=100000)
    parser.add_argument("--semantic-max-yaml-bytes", type=int, default=9000000)
    parser.add_argument(
        "--model",
        default=os.getenv("BIRD_E2E_MODEL"),
        help="override MODEL.<section>.params.model",
    )
    parser.add_argument("--api-base", default=os.getenv("BIRD_E2E_API_BASE"), type=normalize_api_base)
    parser.add_argument(
        "--thinking",
        choices=("enabled", "disabled", "omit"),
        default=os.getenv("BIRD_E2E_THINKING", "omit"),
    )
    parser.add_argument(
        "--enable-thinking",
        nargs="?",
        const="true",
        choices=("true", "false", "omit"),
        default=os.getenv("BIRD_E2E_ENABLE_THINKING", "omit"),
        help="legacy reasoning flag; bare option means true, default omits the field",
    )
    parser.add_argument("--reasoning-effort", default=os.getenv("BIRD_E2E_REASONING_EFFORT"))
    parser.add_argument("--extra-body", type=_extra_body_argument, default=os.getenv("BIRD_E2E_EXTRA_BODY", "{}"))
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=float(os.environ["BIRD_E2E_LLM_TIMEOUT"]) if os.getenv("BIRD_E2E_LLM_TIMEOUT") else None,
        help="override MODEL params.timeout in seconds",
    )
    parser.add_argument(
        "--llm-num-retries",
        type=int,
        default=int(os.environ["BIRD_E2E_LLM_NUM_RETRIES"]) if os.getenv("BIRD_E2E_LLM_NUM_RETRIES") else None,
        help="override MODEL params.num_retries",
    )
    parser.add_argument(
        "--case-timeout",
        type=_optional_limit,
        default=os.getenv("BIRD_E2E_CASE_TIMEOUT", "none"),
        help="deadline in seconds for one complete Agent chat, or none for no whole-case deadline",
    )
    parser.add_argument(
        "--generator-max-tokens",
        type=lambda value: _optional_limit(value, integer=True),
        default=os.getenv("BIRD_E2E_GENERATOR_MAX_TOKENS", "none"),
    )
    parser.add_argument(
        "--llm-max-concurrency",
        type=lambda value: _optional_limit(value, integer=True),
        default=os.getenv("BIRD_E2E_LLM_MAX_CONCURRENCY", "none"),
        help="this worker's BIRD client-call limit, or none",
    )
    parser.add_argument(
        "--semantic-preprocess-model",
        default=os.getenv("BIRD_SEMANTIC_PREPROCESS_MODEL", "DeepSeek-V4-Flash-0731"),
        help="model provenance for the imported canonical semantic model",
    )
    parser.add_argument(
        "--semantic-model-digest",
        default=os.getenv("BIRD_SEMANTIC_MODEL_DIGEST", "unversioned"),
        help="digest of the imported descriptions/OSI/train artifacts for run records",
    )
    parser.add_argument(
        "--semantic-db-prefix",
        default=os.getenv("BIRD_SEMANTIC_DB_PREFIX", "bird"),
        help="semantic-service namespace prefix; use a unique value for isolated imports",
    )
    parser.add_argument("--list", action="store_true", help="List selected questions without running the agent")
    args = parser.parse_args()
    if (
        not args.list
        and not args.semantic_preprocess_provenance
        and (not args.semantic_service_url or not args.semantic_service_url.strip())
    ):
        parser.error("evaluation requires --semantic-service-url or BIRD_SEMANTIC_SERVICE_URL/SEMANTIC_SERVICE_URL")
    return args


def _selected_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    dev_json = args.dev_json or args.bird_data_dir / "dev.json"
    return select_questions(
        _load_questions(dev_json),
        db_ids=args.db_ids,
        question_ids=args.question_ids,
        question_nos=args.question_nos,
    )


def main() -> None:
    args = _parse_args()
    if args.semantic_preprocess_provenance:
        if args.semantic_preprocess_root is None or args.train_cache is None:
            raise ValueError("semantic provenance mode requires --semantic-preprocess-root and --train-cache")
        semantic_preprocess_provenance(
            args.semantic_preprocess_provenance,
            root=args.semantic_preprocess_root,
            train_cache=args.train_cache,
            bird_data_dir=args.bird_data_dir,
            model=args.semantic_preprocess_model,
            prefix=args.semantic_db_prefix,
            value_mode=args.semantic_value_mode,
            text_distinct_max_cardinality=args.semantic_text_distinct_max_cardinality,
            max_values_per_column=args.semantic_max_values_per_column,
            max_values_per_db=args.semantic_max_values_per_db,
            max_yaml_bytes=args.semantic_max_yaml_bytes,
        )
        return
    selected = _selected_from_args(args)
    if args.list:
        for index, item in enumerate(selected, 1):
            print(f"{index:4d}  {item['db_id']:<28} q{item['question_id']}: {item['question']}")
        return
    run_root, results = asyncio.run(
        run_evaluation(
            bird_data_dir=args.bird_data_dir,
            questions=selected,
            output_root=args.output_root,
            semantic_service_url=args.semantic_service_url,
            model_name=args.model,
            api_base=args.api_base,
            thinking=args.thinking,
            enable_thinking=args.enable_thinking,
            reasoning_effort=args.reasoning_effort,
            extra_body=args.extra_body,
            llm_timeout=args.llm_timeout,
            llm_num_retries=args.llm_num_retries,
            case_timeout=args.case_timeout,
            generator_max_tokens=args.generator_max_tokens,
            llm_max_concurrency=args.llm_max_concurrency,
            semantic_preprocess_model=args.semantic_preprocess_model,
            semantic_model_digest=args.semantic_model_digest,
            semantic_db_prefix=args.semantic_db_prefix,
            run_dir=args.run_dir,
            resume=args.resume,
        )
    )
    print(f"BIRD E2E artifacts: {run_root}")
    if not args.no_assert:
        _assert_all_correct(run_root, results)


if __name__ == "__main__":
    main()
