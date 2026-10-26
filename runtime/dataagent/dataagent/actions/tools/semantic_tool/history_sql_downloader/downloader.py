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
"""History-SQL corpus downloader core logic.

Downloads every ``sql_process`` entity from the semantic service via
``GET search/basic`` paging and materializes it as a local read-only corpus.
Every record with a derivable name is written - records are never
deduplicated, the filename is the only discriminator: uppercased output
table name + ``.sql`` (collision groups get a ``_HASH12`` suffix on every
member), scheduler ``$`` placeholders stripped (``temp.x_$date`` ->
``X_DATE.sql``), and DIRECTORY (non-table) outputs named by the record's
sql_id name part (``GET_DATA_..._START_<hash>`` -> ``GET_DATA_..._START.sql``).
The manifest (with per-file sha256) protects pruning: files are only removed
on refresh when a previous manifest proves the directory is downloader-managed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from loguru import logger

from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.utils.runtime_paths import resolve_history_sql_corpus_path

SQL_PROCESS_TYPE_NAME = "sql_process"
MANIFEST_NAME = ".history_sql_manifest.json"
MAX_PAGE_SIZE = 200
MAX_TABLE_NAME_LENGTH = 180

_FILENAME_RE = re.compile(r"[A-Z0-9_]+")
_HASH12_RE = re.compile(r"[0-9a-fA-F]{12}")
_TABLE_FALLBACK_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"INSERT\s+(?:OVERWRITE\s+|INTO\s+)?(?:TABLE\s+)?`?(?P<table>[A-Za-z0-9_.]+)`?", re.IGNORECASE),
    (r"LOAD\s+DATA.*?INTO\s+TABLE\s+`?(?P<table>[A-Za-z0-9_.]+)`?", re.IGNORECASE | re.DOTALL),
    (r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(?P<table>[A-Za-z0-9_.]+)`?", re.IGNORECASE),
    (r"ALTER\s+TABLE\s+`?(?P<table>[A-Za-z0-9_.]+)`?", re.IGNORECASE),
)


class CorpusConfigError(Exception):
    """Agent config reading/validation failure (CLI exit code 2)."""


class CorpusDownloadError(Exception):
    """Network or write failure (CLI exit code 1)."""


@dataclass
class FetchResult:
    """Paged enumeration outcome; guid set is pagination-dup protection only."""

    entities: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    duplicates_skipped: int = 0


@dataclass
class CorpusWriteStats:
    """What one corpus refresh did on disk."""

    written: int = 0
    bare_names: int = 0
    group_suffixed: int = 0
    fallback_named: int = 0
    total_fetched: int = 0
    duplicates_skipped: int = 0
    pages: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    output_dir: str = ""


@dataclass
class CompareReport:
    """Filename-level diff between the downloaded corpus and a reference dir."""

    shared: list[str] = field(default_factory=list)
    shared_content_diff: list[str] = field(default_factory=list)
    server_only: list[str] = field(default_factory=list)
    corpus_only: list[str] = field(default_factory=list)


def load_agent_config(config_path: str | Path) -> dict[str, Any]:
    """Read the agent YAML directly (no ConfigManager; values must be literals)."""
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise CorpusConfigError(f"config 文件不存在：{path}")
    try:
        with path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except yaml.YAMLError as err:
        raise CorpusConfigError(f"config YAML 解析失败（{path}）：{err}") from err
    if not isinstance(config, dict):
        raise CorpusConfigError(f"config 顶层必须是 mapping：{path}")
    return config


def history_sql_enabled_in_config(config: dict[str, Any]) -> bool:
    """Return HISTORY_SQL.enabled from a loaded config; defaults to false."""
    section = config.get("HISTORY_SQL")
    value = section.get("enabled", False) if isinstance(section, dict) else False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def build_client(config: dict[str, Any]) -> SemanticServiceClient:
    """Build a semantic client from the config's SEMANTIC_LAYER section."""
    settings = config.get("SEMANTIC_LAYER")
    if not isinstance(settings, dict) or not settings.get("base_url"):
        raise CorpusConfigError("SEMANTIC_LAYER.base_url 未配置（downloader 直读 yaml，不支持 ${} 插值，值须为字面量）")
    try:
        return SemanticServiceClient.from_settings(settings)
    except ValueError as err:
        raise CorpusConfigError(str(err)) from err


def fetch_all_entities(
    client: SemanticServiceClient,
    *,
    page_size: int = MAX_PAGE_SIZE,
    max_attempts: int = 3,
    max_stagnant_pages: int = 3,
) -> FetchResult:
    """Enumerate all sql_process entities by stepping offset until an empty page.

    The service treats ``(offset, limit)`` page-based (``offset // limit`` is
    the page index), so offset must advance by ``page_size`` - probing showed
    e.g. offset=795/limit=200 re-serves page 3. Retries each page on transport
    errors / 5xx; circuit-breaks when several consecutive pages yield zero
    new guids (offsets paging degenerated). The guid set is pagination-dup
    protection only, never record dedup.
    """
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    result = FetchResult()
    seen_guids: set[str] = set()
    offset = 0
    stagnant_pages = 0
    while True:
        page = _fetch_page(client, offset=offset, page_size=page_size, max_attempts=max_attempts)
        result.pages += 1
        if not page:
            break
        new_count = 0
        for entity in page:
            guid = str(entity.get("guid") or "")
            if guid:
                if guid in seen_guids:
                    result.duplicates_skipped += 1
                    continue
                seen_guids.add(guid)
            result.entities.append(entity)
            new_count += 1
        if new_count:
            stagnant_pages = 0
        else:
            stagnant_pages += 1
            if stagnant_pages >= max_stagnant_pages:
                raise CorpusDownloadError(
                    f"分页熔断：连续 {max_stagnant_pages} 页零新增记录（offset={offset} 起全部为重复 guid）,"
                    "服务端 offset 分页疑似失效，中止以防死循环"
                )
        offset += page_size
    return result


def _fetch_page(
    client: SemanticServiceClient,
    *,
    offset: int,
    page_size: int,
    max_attempts: int,
) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.search_entities(SQL_PROCESS_TYPE_NAME, limit=page_size, offset=offset)
        except httpx.HTTPStatusError as err:
            status = getattr(err.response, "status_code", None)
            if status is None or not 500 <= status < 600:
                raise CorpusDownloadError(f"search/basic 请求失败：HTTP {status}（offset={offset}）") from err
            last_error = err
        except (httpx.TransportError, ValueError) as err:
            last_error = err
        if attempt < max_attempts:
            time.sleep(2 ** (attempt - 1))
    raise CorpusDownloadError(
        f"search/basic 重试 {max_attempts} 次仍失败（offset={offset}）：{last_error}"
    ) from last_error


def _base_name(table: str) -> str:
    """Filename stem for a target: last dot segment, uppercased, ``$`` placeholders stripped.

    Stripping ``$`` mirrors the hand-maintained corpus convention
    (``temp.tmp_x_$date`` -> ``TMP_X_DATE.sql``).
    """
    return table.rsplit(".", 1)[-1].upper().replace("$", "")


def _sql_record_name(attributes: dict[str, Any]) -> str | None:
    """Record name from ``sql_id`` (``NAME_<12hex`` -> ``NAME``); None when absent."""
    sql_id = attributes.get("sql_id")
    if not isinstance(sql_id, str) or not sql_id.strip():
        return None
    stem, _, tail = sql_id.rpartition("_")
    if _HASH12_RE.fullmatch(tail):
        return stem
    return sql_id.strip()


def derive_output_table(entity:dict[str, Any]) -> tuple[str | None, str]:
    """Return (table, outcome); outcome is "target" | "fallback" | "skip:<reason>".

    "fallback" covers names derived rather than taken from a table target:
    tables parsed out of the expression (target_tables is null) and DIRECTORY
    (non-table) outputs, which are named by the record's sql_id name part -
    the hand-maintained corpus convention (GET_DATA_FROM_APP_QA_INFO_START.sql).
    """
    attributes = entity.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    status = str(entity.get("status") or attributes.get("status") or "").strip().upper()
    if status == "DELETED":
        return None, "skip:deleted"
    target_table = _clean_table_name(attributes.get("target_table"))
    if target_table:
        if target_table.upper().startswith("DIRECTORY") or "://" in target_table:
            record_name = _sql_record_name(attributes)
            if record_name:
                return record_name, "fallback"
            return None, "skip:not-a-table"
        return target_table, "target"
    expression = attributes.get("expression")
    if isinstance(expression, str) and expression.strip():
        for pattern, flags in _TABLE_FALLBACK_PATTERNS:
            match = re.search(pattern, expression, flags)
            if match:
                table = _clean_table_name(match.group("table"))
                if table:
                    return table, "fallback"
    return None, "skip:no-table"


def _clean_table_name(raw: Any) -> str:
    return str(raw or "").strip().strip("`\"'").strip()


def build_output_filename(
    table: str,
    *,
    multi: bool,
    used: set[str],
    sql_id: str | None,
    guid: str,
) -> str:
    """Corpus filename for one record; records are never deduplicated.

    bare name for singleton tables; every member of a collision group gets a
    ``_HASH12`` suffix (no bare name survives in a group). ``$`` placeholders
    are stripped from the base (``temp.x_$date`` -> ``X_DATE.sql``). ``used``
    only guards the pathological same-base+same-hash collision.
    """
    base = _base_name(table)
    if len(base) > MAX_TABLE_NAME_LENGTH or not _FILENAME_RE.fullmatch(base):
        raise ValueError(f"表名不符合语料文件命名约束（[A-Z0-9_]+ 且 ≤{MAX_TABLE_NAME_LENGTH} 字符）：{table!r}")
    name = f"{base}.sql" if not multi else f"{base}_{_hash12(sql_id, guid)}.sql"
    if name in used:
        stem = name[: -len(".sql")]
        suffix = 2
        while f"{stem}_{suffix}.sql" in used:
            suffix += 1
        name = f"{stem}_{suffix}.sql"
    used.add(name)
    return name


def _hash12(sql_id: str | None, guid: str) -> str:
    if sql_id:
        tail = str(sql_id).rsplit("_", 1)[-1]
        if _HASH12_RE.fullmatch(tail):
            return tail.lower()
    return hashlib.sha256(str(guid).encode("utf-8")).hexdigest()[:12]


def write_corpus(
    entities: list[dict[str, Any]],
    output_dir: str | Path,
    *,
    lock: bool = True,
    source_base_url: str = "",
) -> CorpusWriteStats:
    """Materialize the corpus: unlock -> write -> manifest-prune -> manifest -> lock."""
    output_dir = Path(output_dir)
    stats = CorpusWriteStats()
    stats.output_dir = str(output_dir)

    ordered = sorted(entities, key=lambda entity: str(entity.get("guid") or ""))
    derived: list[tuple[dict[str, Any], str, str]] = []
    base_counts: Counter[str] = Counter()
    for entity in ordered:
        table, outcome = derive_output_table(entity)
        if table is None:
            stats.skipped.append((str(entity.get("guid") or "?"), outcome))
            continue
        attributes = entity.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        if not str(attributes.get("expression") or "").strip():
            stats.skipped.append((str(entity.get("guid") or "?"), "skip:empty-expression"))
            continue
        derived.append((entity, table, outcome))
        base_counts[_base_name(table)] += 1

    _unlock_tree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_tmp in output_dir.glob(".*.tmp"):
        try:
            stale_tmp.unlink()
        except OSError as err:
            logger.warning("history_sql_downloader: 清理残留 tmp 失败 {}: {}", stale_tmp.name, err)

    try:
        used: set[str] = set()
        manifest_files: list[dict[str, str]] = []
        for entity, table, outcome in derived:
            attributes = entity.get("attributes")
            attributes = attributes if isinstance(attributes, dict) else {}
            try:
                name = build_output_filename(
                    table,
                    multi=base_counts[_base_name(table)] > 1,
                    used=used,
                    sql_id=attributes.get("sql_id"),
                    guid=str(entity.get("guid") or ""),
                )
            except ValueError:
                stats.skipped.append((str(entity.get("guid") or "?"), "skip:invalid-name"))
                continue
            expression = str(attributes.get("expression") or "")
            content = expression if not expression or expression.endswith("\n") else expression + "\n"
            tmp_path = output_dir / f".{name}.tmp"
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, output_dir / name)
            manifest_files.append({"file": name, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()})
            stats.written += 1
            if outcome == "fallback":
                stats.fallback_named += 1
            if name == f"{_base_name(table)}.sql":
                stats.bare_names += 1
            else:
                stats.group_suffixed += 1

        manifest_path = output_dir / MANIFEST_NAME
        current_names = {record["file"] for record in manifest_files}
        if manifest_path.is_file():
            for existing in sorted(output_dir.glob("*.sql")):
                if existing.name in current_names:
                    continue
                try:
                    existing.unlink()
                    stats.pruned.append(existing.name)
                except OSError as err:
                    logger.warning("history_sql_downloader: prune 删除失败 {}: {}", existing.name, err)

        manifest = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": source_base_url,
            "files": manifest_files,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if lock:
            _lock_tree(output_dir)
    return stats


def compare_corpora(output_dir: str | Path, compare_dir: str | Path) -> CompareReport:
    """Filename-level diff, plus content check on shared names."""
    output_dir = Path(output_dir)
    compare_dir = Path(compare_dir)
    output_names = {path.name for path in output_dir.glob("*.sql")}
    compare_names = {path.name for path in compare_dir.glob("*.sql")}
    report = CompareReport()
    report.shared = sorted(output_names & compare_names)
    report.server_only = sorted(output_names - compare_names)
    report.corpus_only = sorted(compare_names - output_names)
    for name in report.shared:
        if (output_dir / name).read_bytes() != (compare_dir / name).read_bytes():
            report.shared_content_diff.append(name)
    return report


def downloader_full_corpus(
    config: dict[str, Any],
    *,
    output_dir: str | Path | None = None,
    page_size: int = MAX_PAGE_SIZE,
    lock: bool = True,
) -> CorpusWriteStats:
    """Full pipeline: config -> client -> fetch -> write; raises on failure."""
    client = build_client(config)
    resolve_dir = Path(output_dir).expanduser().resolve() if output_dir else resolve_history_sql_corpus_path()
    try:
        fetch_result = fetch_all_entities(client, page_size=page_size)
        if fetch_result.duplicates_skipped > 0:
            logger.warning(
                "历史SQL分页出现重复 guid（{} 条）：服务端行序可能漂移，本次语料可能不完整，建议重跑核对",
                fetch_result.duplicates_skipped,
            )
        stats = write_corpus(fetch_result.entities, resolve_dir, lock=lock, source_base_url=str(client.base_url))
    except OSError as err:
        raise CorpusDownloadError(f"历史SQL语料下载/写入失败（磁盘/权限）：{err}") from err
    stats.total_fetched = len(fetch_result.entities)
    stats.pages = fetch_result.pages
    stats.duplicates_skipped = fetch_result.duplicates_skipped
    logger.info(
        "历史SQL语料下载完成：拉取 {} 条（{} 页，分页重复跳过 {}）/ 落盘 {} 文件（裸名 {} + 碰撞组后缀 {}，含回退 {}）"
        "/ skip {} / prune {} -> {}",
        stats.total_fetched,
        stats.pages,
        stats.duplicates_skipped,
        stats.written,
        stats.bare_names,
        stats.group_suffixed,
        stats.fallback_named,
        len(stats.skipped),
        len(stats.pruned),
        resolve_dir,
    )
    return stats


def ensure_history_sql_corpus(
    config_path: str | Path,
    *,
    if_enabled: bool = True,
    page_size: int = MAX_PAGE_SIZE,
) -> int:
    """In-process entry for the evaluation bootstrap hook; returns an exit code.

    0 = success (or skipped because the switch is off); 1 = network/write
    failure; 2 = config error. When ``if_enabled`` and HISTORY_SQL.enabled is
    false, this is a zero-action success.
    """
    try:
        config = load_agent_config(config_path)
        if if_enabled and not history_sql_enabled_in_config(config):
            logger.info("HISTORY_SQL.enabled=false：历史SQL功能未启用，跳过语料下载（零动作）")
            return 0
        downloader_full_corpus(config, page_size=page_size, lock=True)
        return 0
    except CorpusConfigError as err:
        logger.error("history_sql_downloader 配置错误：{}", err)
        return 2
    except CorpusDownloadError as err:
        logger.error("history_sql_downloader 下载/写入失败：{}", err)
        return 1


def _unlock_tree(root: Path) -> None:
    """Restore owner rw on the corpus tree before rewriting (top-down)."""
    if not root.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        _chmod_quiet(dirpath, 0o755)
        for filename in filenames:
            _chmod_quiet(os.path.join(dirpath, filename), 0o644)


def _lock_tree(root: Path) -> None:
    """Lock the corpus read-only: dir 555, *.sql 444, other files untouched."""
    if not root.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        _chmod_quiet(dirpath, 0o555)
        for filename in filenames:
            if filename.endswith(".sql"):
                _chmod_quiet(os.path.join(dirpath, filename), 0o444)


def _chmod_quiet(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as err:
        logger.warning("history_sql_downloader: chmod {} -> {:o} 失败：{}", path, mode, err)
