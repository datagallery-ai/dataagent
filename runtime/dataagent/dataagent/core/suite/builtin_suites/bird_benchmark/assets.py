"""Prepare, validate and explicitly import reusable BIRD semantic assets."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from .bird_to_osi_yaml import build_yaml, validate_db_id
from .model_options import normalize_api_base, normalize_extra_body
from .validate_osi_import import validate_import

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


def _selected_ids(db_ids: list[str] | None) -> list[str]:
    selected = list(BIRD_DB_IDS if db_ids is None else db_ids)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("database selection must be nonempty and contain no duplicates")
    for item in selected:
        validate_db_id(item)
    return selected


def _redact(value: str) -> str:
    for name, secret in os.environ.items():
        if secret and name.upper().endswith(("API_KEY", "TOKEN", "PASSWORD", "SECRET")):
            value = value.replace(secret, "[REDACTED]")
    return value


def _write_json(path: Path, value: Any) -> None:
    def scrub(item: Any) -> Any:
        if isinstance(item, str):
            return _redact(item)
        if isinstance(item, dict):
            return {key: scrub(child) for key, child in item.items()}
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item

    path.write_text(json.dumps(scrub(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _service_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("semantic service URL must be HTTP(S), without embedded credentials")
    if parts.query or parts.fragment:
        raise ValueError("semantic service URL cannot contain a query or fragment")
    return value.rstrip("/")


def verify_assets(root: Path, *, semantic_db_prefix: str, db_ids: list[str] | None = None) -> dict:
    """Check old descriptions/OSI without requiring provenance, hashes or new logs."""
    selected = _selected_ids(db_ids)
    if not re.fullmatch(r"[A-Za-z0-9_]+", semantic_db_prefix):
        raise ValueError("semantic database prefix may contain only letters, digits and underscores")
    for directory, pattern in (("descriptions", "*.json"), ("osi", "*.yaml")):
        available = {p.stem for p in (root / directory).glob(pattern) if p.is_file() and not p.name.startswith("._")}
        if not set(selected) <= available or (db_ids is None and available != set(selected)):
            raise ValueError(f"{directory} do not match the selected BIRD databases")
    for db_id in selected:
        desc = json.loads((root / "descriptions" / f"{db_id}.json").read_text(encoding="utf-8"))
        if (
            not isinstance(desc, dict)
            or not desc
            or any(
                not isinstance(key, str) or "." not in key or not isinstance(value, str) or not value.strip()
                for key, value in desc.items()
            )
        ):
            raise ValueError(f"invalid description cache for {db_id}")
        document = yaml.safe_load((root / "osi" / f"{db_id}.yaml").read_text(encoding="utf-8"))
        models = document.get("semantic_model", []) if isinstance(document, dict) else []
        namespace = f"{semantic_db_prefix}_{db_id}"
        if (
            len(models) != 1
            or not isinstance(models[0], dict)
            or models[0].get("name") != f"{namespace}_semantic_model"
        ):
            raise ValueError(f"semantic model namespace mismatch for {db_id}")

        def qualified(value: Any, namespace: str = namespace) -> bool:
            return isinstance(value, str) and value.startswith(f"{namespace}.") and len(value) > len(namespace) + 1

        datasets = models[0].get("datasets")
        if not isinstance(datasets, list) or not datasets:
            raise ValueError(f"semantic datasets missing for {db_id}")
        for dataset in datasets:
            extension = dataset.get("custom_extensions", {})
            if not all(qualified(dataset.get(key)) for key in ("name", "source")) or any(
                extension.get(key) != namespace for key in ("schema_name", "db_name_en")
            ):
                raise ValueError(f"semantic dataset namespace mismatch for {db_id}")
            fields = dataset.get("fields")
            if not isinstance(fields, list) or not fields:
                raise ValueError(f"semantic fields missing for {db_id}")
            table = dataset["name"][len(namespace) + 1 :]
            for field in fields:
                extension = field.get("custom_extensions", {})
                if extension.get("db_name_en") != namespace or (
                    "qualified_name" in extension and not qualified(extension["qualified_name"])
                ):
                    raise ValueError(f"semantic field namespace mismatch for {db_id}")
                if f"{table}.{field.get('name')}" not in desc:
                    raise ValueError(f"missing cached field description for {db_id}")
        extensions = models[0].get("custom_extensions", [])
        if not isinstance(extensions, list) or len(extensions) != 1:
            raise ValueError(f"semantic graph extension missing for {db_id}")
        graph = extensions[0].get("data", {}).get("graph", {})
        nodes = graph.get("nodes", {})
        for value in nodes.get("column_values", []):
            if not qualified(value.get("physical_ref", {}).get("dataset")):
                raise ValueError(f"semantic column-value namespace mismatch for {db_id}")
        for process in nodes.get("sql_processes", []):
            if not qualified(process.get("qualified_name")) or process.get("source_tables") != [namespace]:
                raise ValueError(f"semantic SQL-process namespace mismatch for {db_id}")
        for family, source, target, expression in (
            ("table_join_relationship", "source_entity", "target_entity", "join_condition"),
            ("column_join_relationship", "source_dimension", "target_dimension", "expression"),
        ):
            for edge in graph.get("edges", {}).get(family, []):
                sides = [side.strip() for side in str(edge.get(expression, "")).split("=")]
                if (
                    not all(qualified(edge.get(key)) for key in (source, target))
                    or len(sides) != 2
                    or not all(map(qualified, sides))
                ):
                    raise ValueError(f"semantic join namespace mismatch for {db_id}")
        stats_path = root / "logs" / f"{db_id}_value_stats.json"
        if stats_path.is_file():
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            if stats.get("db_id") != db_id or stats.get("semantic_db_id") != namespace:
                raise ValueError(f"value stats namespace mismatch for {db_id}")
            if stats.get("totals", {}).get("selected") != len(nodes.get("column_values", [])):
                raise ValueError(f"value stats count mismatch for {db_id}")
    return {"db_ids": selected, "semantic_db_prefix": semantic_db_prefix}


def import_assets(
    root: Path, *, semantic_service_url: str, semantic_db_prefix: str, db_ids: list[str] | None = None
) -> None:
    """Explicitly import verified YAML; reusing assets alone never calls this."""
    selected = verify_assets(root, semantic_db_prefix=semantic_db_prefix, db_ids=db_ids)["db_ids"]
    endpoint = f"{_service_url(semantic_service_url)}/api/semantic/v1/osi/import"
    for directory in ("import_responses", "logs"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for db_id in selected:
        payload = (root / "osi" / f"{db_id}.yaml").read_bytes()
        try:
            response = httpx.post(
                endpoint, content=payload, headers={"Content-Type": "application/x-yaml"}, timeout=1800, trust_env=False
            )
            response.raise_for_status()
            body = response.json()
            _write_json(root / "import_responses" / f"{db_id}.json", body)
            counts = validate_import(yaml.safe_load(payload), body)
            _write_json(root / "logs" / f"{db_id}_import_validation.json", counts)
        except Exception as exc:
            raise RuntimeError(f"OSI import failed for {db_id}: {_redact(str(exc))}") from None


def prepare_assets(
    *,
    bird_data_dir: Path,
    train_cache: Path,
    output_root: Path,
    semantic_service_url: str,
    semantic_db_prefix: str,
    model: str,
    api_base: str,
    thinking: str = "omit",
    enable_thinking: str = "omit",
    reasoning_effort: str | None = None,
    extra_body: dict | None = None,
    llm_timeout: float = 900,
    llm_num_retries: int = 2,
    db_ids: list[str] | None = None,
    value_mode: str = "text_distinct",
    text_distinct_max_cardinality: int = 1000,
    max_values_per_column: int = 10000,
    max_values_per_db: int = 100000,
    max_yaml_bytes: int = 9000000,
) -> Path:
    """Reuse the existing description, OSI and vector-import algorithms."""
    selected = _selected_ids(db_ids)
    api_base = normalize_api_base(api_base)
    _service_url(semantic_service_url)
    normalized = normalize_extra_body(
        thinking=thinking, enable_thinking=enable_thinking, reasoning_effort=reasoning_effort, extra_body=extra_body
    )
    if llm_timeout <= 0 or llm_num_retries < 0:
        raise ValueError("preprocessing HTTP timeout must be positive and retries nonnegative")
    if not re.fullmatch(r"[A-Za-z0-9_]+", semantic_db_prefix):
        raise ValueError("semantic database prefix may contain only letters, digits and underscores")
    for directory in ("descriptions", "osi", "import_responses", "logs"):
        (output_root / directory).mkdir(parents=True, exist_ok=True)
    settings = {
        "model": model,
        "api_base": api_base,
        "extra_body": normalized,
        "llm_timeout": llm_timeout,
        "llm_num_retries": llm_num_retries,
    }
    provenance: dict[str, Any] = {
        "requested_settings": settings,
        "semantic_db_prefix": semantic_db_prefix,
        "databases": {},
    }
    for db_id in selected:
        desc_path = output_root / "descriptions" / f"{db_id}.json"
        previous = json.loads(desc_path.read_text(encoding="utf-8")) if desc_path.exists() else {}
        command = [
            sys.executable,
            "-m",
            f"{__package__}.generate_llm_column_desc",
            "--db-id",
            db_id,
            "--bird-dir",
            str(bird_data_dir / "dev_databases"),
            "--output",
            str(desc_path),
            "--api-base",
            api_base,
            "--model",
            model,
            "--read-timeout",
            str(llm_timeout),
            "--max-attempts",
            str(llm_num_retries + 1),
            "--thinking",
            thinking,
            "--enable-thinking",
            str(enable_thinking).lower(),
        ]
        if reasoning_effort is not None:
            command += ["--reasoning-effort", reasoning_effort]
        if extra_body is not None:
            command += ["--extra-body", json.dumps(extra_body)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        (output_root / "logs" / f"{db_id}_descriptions.log").write_text(
            _redact(result.stdout + result.stderr), encoding="utf-8"
        )
        if result.returncode:
            raise RuntimeError(f"description generation failed for {db_id}; see logs/{db_id}_descriptions.log")
        current = json.loads(desc_path.read_text(encoding="utf-8"))
        provenance["databases"][db_id] = {
            "generated_descriptions": sum(value != previous.get(key) for key, value in current.items()),
            "reused_descriptions": sum(value == previous.get(key) for key, value in current.items()),
        }
        stats: dict[str, Any] = {}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            document = build_yaml(
                db_id,
                bird_data_dir / "dev_databases",
                bird_data_dir / "dev_tables.json",
                train_cache,
                llm_desc_cache=desc_path,
                semantic_db_id=f"{semantic_db_prefix}_{db_id}",
                train_cache_mode="all" if db_id == "california_schools" else "none",
                value_mode=value_mode,
                text_distinct_max_cardinality=text_distinct_max_cardinality,
                max_values_per_column=max_values_per_column,
                max_values_per_db=max_values_per_db,
                max_yaml_bytes=max_yaml_bytes,
                stats=stats,
            )
        (output_root / "logs" / f"{db_id}_yaml.log").write_text(_redact(output.getvalue()), encoding="utf-8")
        (output_root / "osi" / f"{db_id}.yaml").write_text(
            yaml.dump(document, allow_unicode=True, sort_keys=False, width=4096), encoding="utf-8"
        )
        _write_json(output_root / "logs" / f"{db_id}_value_stats.json", stats)
        _write_json(output_root / "semantic_preprocess_settings.json", provenance)
        import_assets(
            output_root,
            semantic_service_url=semantic_service_url,
            semantic_db_prefix=semantic_db_prefix,
            db_ids=[db_id],
        )
    return output_root
