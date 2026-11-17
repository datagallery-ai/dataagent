#!/usr/bin/env python3
"""step2_3b: config-driven semantic prior feature extension (optional stage).

Runs between step2_3 and step2_4 when (and only when) the task instruction carries a
semantic feature config reference (a ``semantic_feature_config: <path>`` key, in any
form -- a dedicated bracket-tag line or an entry in a parameter list).
Without a config the pipeline is unchanged; nothing in this script runs.

The generator is scene-agnostic: every feature's SQL and metadata comes from the
config's ``feature_blocks`` (see ``references/semantic_feature_config.example.yaml``).
This script only validates the grammar, substitutes placeholders and assembles —
it never invents SQL, never connects to the warehouse, and its artifacts must never
be hand-edited (fix the config or the task parameters, then re-run the generator).

Sub-commands:

  * ``generate``       -- validate the config, render the 5 artifacts + run manifest.
  * ``check``          -- config health check only; writes nothing.
  * ``gate``           -- decide the step2_3b validation SELECT booleans and write
                          ``progress_step2_3b.txt`` (the stage's recovery marker).
  * ``spearman-gate``  -- after the step2_4 CSV export: drop ``sem_`` columns that
                          duplicate an old numeric feature (avg-rank Spearman),
                          atomically rewrite the CSV + contract fragment, and write
                          the gate reports.
  * ``merge-contract`` -- merge the (gated) fragment into the deployment contract.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from step2_driver_common import (  # noqa: E402
    bind_placeholders,
    load_data_source,
    unbound_placeholders,
    write_manifest,
)
from step2_3_expand_sql import (  # noqa: E402
    _extract_results,
    _load_payloads,
    _non_comment_text,
    _plain_records,
    _read_json,
)

# Fixed artifact / table names (current-environment naming).
INPUT_TABLE = "step2_3_wide_cleaned"
SEMANTIC_TABLE = "step2_3b_wide_semantic"
APPLY_JSON = "semantic_apply.json"
SEMANTIC_SQL = "step2_3b_semantic_features.sql"
VALIDATION_SQL = "step2_3b_validation.sql"
FRAGMENT_JSON = "step2_3b_contract_fragment.json"
SEMANTIC_MD = "step2_3b_semantic_features.md"
MANIFEST = "step2_3b_run.json"
RAW_VALIDATION = "raw_step2_3b_validation.json"
MARKER = "progress_step2_3b.txt"
GATE_REPORT_JSON = "step2_3b_gate_report.json"
GATE_REPORT_MD = "step2_3b_gate_report.md"
WIDE_CSV = "step2_4_wide_userfiltered.csv"
CONTRACT = "step2_2_deployment_feature_contract.json"
SCHEMA = "schema_resolution.json"
SAMPLE_STATS = "step1_sample_stats.json"

ALLOWED_DTYPES = ("UInt64", "UInt8", "Float64")
DEFAULT_SPEARMAN_GATE = 0.6
_CHECK = "python skill/feature-engineer/scripts/step2_driver_common.py check-collect"

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FEATURE_NAME_RE = re.compile(r"^sem_[A-Za-z0-9_]+$")
PARAMETER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
PROFILE_RE = re.compile(r"\{\{\s*profile\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
VOCAB_RE = re.compile(r"\{\{\s*vocab\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# Mirrors step2_2_validate_deployment_contract._expression_references so the
# generator's source_columns cross-check matches the validator's exact rules.
QUALIFIED_COLUMN_RE = re.compile(
    r"(?<![A-Za-z0-9_])`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\.\s*`?([A-Za-z_][A-Za-z0-9_]*)`?"
)
_FROM_RE = re.compile(
    r"^FROM\s+\{\{\s*source_db\s*\}\}\.([A-Za-z_][A-Za-z0-9_]*)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)$",
    re.IGNORECASE,
)
_JOIN_RE = re.compile(
    r"^((?:INNER|LEFT)\s+JOIN)\s+\{\{\s*source_db\s*\}\}\.([A-Za-z_][A-Za-z0-9_]*)"
    r"\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)\s+ON\s+(.+)$",
    re.IGNORECASE,
)
_WHERE_RE = re.compile(r"^WHERE\s+(.+)$", re.IGNORECASE)
_GROUP_RE = re.compile(
    r"^GROUP\s+BY\s+([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$", re.IGNORECASE
)
_ANCHOR_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*),?$"
)
# Greedy prefix -> the LAST "AS <name>" at end of line anchors the feature name.
_FEATURE_LINE_RE = re.compile(r"^(.*\S)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)\s*,?\s*$")
_SELECT_TOKEN_RE = re.compile(r"\bselect\b", re.IGNORECASE)


class ConfigError(Exception):
    """A config/grammar violation with a block-prefixed, human-readable message."""


@dataclass
class BlockFeature:
    name: str
    dtype: str
    source_columns: list[tuple[str, str]]
    description: str


@dataclass
class FeatureBlock:
    block: str
    user_col: str
    source_table: str
    source_alias: str
    join: tuple[str, str, str, str] | None  # (type, table, alias, on)
    where: str | None
    anchor_line: str
    declared_names: set[str] = field(default_factory=set)  # enabled + disabled metadata names
    feature_lines: list[tuple[str, str]] = field(default_factory=list)  # (raw expr, name)
    features: list[BlockFeature] = field(default_factory=list)
    # Raw section lines (config text, verbatim) so the generated SQL stays
    # faithful to the config — only placeholders are replaced.
    from_line: str = ""
    join_line: str = ""
    where_line: str = ""
    group_line: str = ""


# ---------------------------------------------------------------------------
# atomic writers
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            stream.write(text)
            temporary = Path(stream.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sql_literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _expression_references(expression: str) -> list[tuple[str, str]]:
    """Same ref-extraction rule as the deployment-contract validator."""
    text = re.sub(r"/\*.*?\*/", " ", expression, flags=re.DOTALL)
    text = re.sub(r"--[^\r\n]*", " ", text)
    text = re.sub(r"'(?:''|\\.|[^'])*'", "''", text)
    return QUALIFIED_COLUMN_RE.findall(text)


# ---------------------------------------------------------------------------
# config + meta loading
# ---------------------------------------------------------------------------


def _load_config(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment issue
        raise SystemExit(
            "未安装 PyYAML（import yaml 失败），无法解析语义特征配置。"
            "请先安装 pyyaml 再重跑本脚本。"
        ) from exc
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"无法解析语义特征配置 {path}：{exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} 必须是一个 YAML 对象（version/scene/feature_blocks…）")
    if raw.get("version") != 1:
        raise SystemExit(f"{path} 的 version 必须为 1，实际 {raw.get('version')!r}")
    return raw


def _load_meta(path: Path) -> tuple[str, dict[str, set[str]], list[str]]:
    """Return ``(source_database, {table: columns}, user_id_columns)``."""
    meta = _read_json(path)
    source_database = str(meta.get("source_database") or "").strip()
    if not source_database:
        raise SystemExit(f"{path} 缺少 source_database，无法解析 {{source_db}} 占位符")
    tables: dict[str, set[str]] = {}
    for item in meta.get("tables") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        columns = {
            str(column.get("name"))
            for column in item.get("columns") or []
            if isinstance(column, dict) and column.get("name")
        }
        tables[name] = columns
    aliases = meta.get("column_aliases") or {}
    user_id_columns = [
        str(value).strip()
        for value in (aliases.get("user_id_columns") or [])
        if str(value).strip()
    ]
    return source_database, tables, user_id_columns


def _scalar(value: Any, where: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"{where} 的取值必须是标量字符串/数字，实际 {value!r}")
    return str(value)


# ---------------------------------------------------------------------------
# block parsing (grammar + metadata + meta cross-checks)
# ---------------------------------------------------------------------------


def _enabled_set(config: dict[str, Any]) -> set[str] | None:
    """``None`` means "everything enabled" (composite_features omitted/empty)."""
    composite = config.get("composite_features")
    if not composite:
        return None
    if not isinstance(composite, list):
        raise ConfigError("composite_features 必须是列表（{name: …, enabled: …} 项）")
    enabled: set[str] = set()
    for index, entry in enumerate(composite):
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ConfigError(f"composite_features[{index}] 缺少 name")
        name = str(entry["name"]).strip()
        if bool(entry.get("enabled", True)):
            enabled.add(name)
    return enabled


def _parse_feature_metadata(
    block_name: str, raw_features: Any, enabled: set[str] | None
) -> tuple[list[BlockFeature], list[str]]:
    """Return ``(enabled features, disabled names)`` for one block."""
    if not isinstance(raw_features, list) or not raw_features:
        raise ConfigError(f"块 {block_name} 的 features 必须是非空列表")
    features: list[BlockFeature] = []
    disabled: list[str] = []
    for index, raw in enumerate(raw_features):
        where = f"块 {block_name} features[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} 必须是对象（name/dtype/source_columns/description）")
        name = str(raw.get("name") or "").strip()
        if not FEATURE_NAME_RE.fullmatch(name):
            raise ConfigError(
                f"{where}.name={name!r} 非法：必须是 sem_ 前缀且仅含 [A-Za-z0-9_]"
            )
        dtype = str(raw.get("dtype") or "").strip()
        if dtype not in ALLOWED_DTYPES:
            raise ConfigError(f"{where}.dtype 必须是 {'/'.join(ALLOWED_DTYPES)}，实际 {dtype!r}")
        source_columns: list[tuple[str, str]] = []
        raw_columns = raw.get("source_columns")
        if not isinstance(raw_columns, list):
            raise ConfigError(f"{where}.source_columns 必须是 `别名.列名` 字符串列表")
        for entry in raw_columns:
            text = str(entry or "").strip()
            parts = text.split(".", 1)
            if len(parts) != 2 or not IDENTIFIER_RE.fullmatch(parts[0]) or not IDENTIFIER_RE.fullmatch(parts[1]):
                raise ConfigError(f"{where}.source_columns 项 {text!r} 不是 `别名.列名`")
            source_columns.append((parts[0], parts[1]))
        if enabled is not None and name not in enabled:
            disabled.append(name)
            continue
        features.append(
            BlockFeature(
                name=name,
                dtype=dtype,
                source_columns=source_columns,
                description=str(raw.get("description") or "").strip(),
            )
        )
    return features, disabled


def _parse_block_sql(
    block: FeatureBlock, raw_sql: Any, enabled: set[str] | None
) -> None:
    """Fill ``feature_lines`` on *block* from its sql text (grammar-enforced)."""
    if not isinstance(raw_sql, str) or not raw_sql.strip():
        raise ConfigError(f"块 {block.block} 的 sql 必须是非空字符串")
    lines = [line.rstrip() for line in raw_sql.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if any(not line.strip() for line in lines):
        raise ConfigError(f"块 {block.block} 的 sql 含空行（一行一个要素，禁止空行）")
    stripped = [line.strip() for line in lines]
    if stripped[0].upper() != "SELECT":
        raise ConfigError(f"块 {block.block} 的 sql 第一行必须是 SELECT")

    from_line: str | None = None
    join_line: str | None = None
    where_line: str | None = None
    group_line: str | None = None
    select_lines: list[str] = []
    section = "SELECT"
    for line in stripped[1:]:
        if _SELECT_TOKEN_RE.search(line):
            raise ConfigError(f"块 {block.block} 的 sql 含第二个 SELECT（禁止子查询）")
        upper = line.upper()
        if upper.startswith("FROM "):
            if section != "SELECT" or from_line is not None:
                raise ConfigError(f"块 {block.block} 的 FROM 位置或数量不合法")
            from_line = line
            section = "FROM"
        elif re.match(r"^(?:INNER|LEFT)\s+JOIN\s+", upper):
            if section != "FROM" or join_line is not None:
                raise ConfigError(f"块 {block.block} 的 JOIN 位置不合法或超过 1 个（每块至多 1 个 JOIN）")
            join_line = line
            section = "JOIN"
        elif upper.startswith("WHERE "):
            if section not in ("FROM", "JOIN") or where_line is not None:
                raise ConfigError(f"块 {block.block} 的 WHERE 位置或数量不合法")
            where_line = line
            section = "WHERE"
        elif upper.startswith("GROUP BY "):
            if group_line is not None:
                raise ConfigError(f"块 {block.block} 的 GROUP BY 数量不合法")
            group_line = line
            section = "GROUP"
        else:
            if section != "SELECT":
                raise ConfigError(f"块 {block.block} 无法识别的行：{line!r}")
            select_lines.append(line)
    if from_line is None:
        raise ConfigError(f"块 {block.block} 缺少 FROM 行")
    if group_line is None:
        raise ConfigError(f"块 {block.block} 缺少 GROUP BY 行（首键必须是主表用户键）")

    from_match = _FROM_RE.match(from_line)
    if not from_match:
        raise ConfigError(
            f"块 {block.block} 的 FROM 行必须形如 FROM {{{{source_db}}}}.<表> AS <别名>，实际 {from_line!r}"
        )
    block.source_table, block.source_alias = from_match.group(1), from_match.group(2)
    block.from_line = from_line
    if join_line is not None:
        join_match = _JOIN_RE.match(join_line)
        if not join_match:
            raise ConfigError(
                f"块 {block.block} 的 JOIN 行必须形如 [INNER|LEFT JOIN] {{{{source_db}}}}.<表> AS <别名> ON <条件>，"
                f"实际 {join_line!r}"
            )
        block.join = (
            " ".join(join_match.group(1).upper().split()),
            join_match.group(2),
            join_match.group(3),
            join_match.group(4).strip(),
        )
        block.join_line = join_line
    if where_line is not None:
        where_match = _WHERE_RE.match(where_line)
        if not where_match:
            raise ConfigError(f"块 {block.block} 的 WHERE 行非法：{where_line!r}")
        block.where = where_match.group(1).strip()
        block.where_line = where_line
    if group_line is not None:
        block.group_line = group_line

    group_match = _GROUP_RE.match(group_line)
    if not group_match:
        raise ConfigError(
            f"块 {block.block} 的 GROUP BY 行必须形如 GROUP BY <主表别名>.<user_col>，实际 {group_line!r}"
        )
    if group_match.group(1) != block.source_alias or group_match.group(2) != block.user_col:
        raise ConfigError(
            f"块 {block.block} 的 GROUP BY 首键必须是 {block.source_alias}.{block.user_col}"
        )

    if not select_lines:
        raise ConfigError(f"块 {block.block} 的 SELECT 段为空（缺少用户键锚定行）")
    anchor = _ANCHOR_RE.match(select_lines[0])
    if not anchor:
        raise ConfigError(
            f"块 {block.block} 的 SELECT 首行必须是 <主表别名>.<user_col> AS <user_col>，实际 {select_lines[0]!r}"
        )
    if anchor.group(1) != block.source_alias or anchor.group(2) != block.user_col or anchor.group(3) != block.user_col:
        raise ConfigError(
            f"块 {block.block} 的 SELECT 首行用户键必须是 {block.source_alias}.{block.user_col}"
        )
    block.anchor_line = select_lines[0]

    # Feature lines: disabled ones are recognized (grammar-valid) then dropped
    # before any placeholder validation/substitution.
    declared = block.declared_names
    seen_lines: set[str] = set()
    for line in select_lines[1:]:
        match = _FEATURE_LINE_RE.match(line)
        if not match:
            raise ConfigError(f"块 {block.block} 的特征行缺少行尾 `AS <特征名>`：{line!r}")
        expression, name = match.group(1).strip(), match.group(2)
        if name not in declared:
            raise ConfigError(
                f"块 {block.block} 的 SELECT 行特征 {name!r} 未在 features 元数据中声明"
            )
        if name in seen_lines:
            raise ConfigError(f"块 {block.block} 的特征 {name} 在 SELECT 中重复出现")
        seen_lines.add(name)
        if enabled is not None and name not in enabled:
            continue
        block.feature_lines.append((expression, name))

    missing = sorted({feature.name for feature in block.features} - seen_lines)
    if missing:
        raise ConfigError(
            f"块 {block.block} 启用的特征缺少 SELECT 行：" + ", ".join(missing)
        )
    # keep metadata order aligned with the SELECT order
    order = {name: index for index, (_, name) in enumerate(block.feature_lines)}
    block.features.sort(key=lambda feature: order[feature.name])


def _check_refs(
    block: FeatureBlock,
    substitute: _Substituter,
    meta_tables: dict[str, set[str]],
    errors: list[str],
) -> None:
    """Cross-check every alias.column reference on the SUBSTITUTED text (the exact
    text the deployment-contract validator will see) against the step1 meta schema."""
    aliases = {block.source_alias: block.source_table}
    if block.join is not None:
        _, join_table, join_alias, _ = block.join
        if join_alias in aliases:
            errors.append(f"块 {block.block}: JOIN 别名 {join_alias} 与主表别名重复")
        aliases[join_alias] = join_table
    for alias, table in aliases.items():
        if table not in meta_tables:
            errors.append(f"块 {block.block}: 表 {table} 不在 step1 meta 的表清单中")

    def _check(text: str, where: str) -> None:
        for ref_alias, column in _expression_references(substitute(text)):
            table = aliases.get(ref_alias)
            if table is None:
                errors.append(f"块 {block.block}.{where}: 未声明的别名 {ref_alias!r}")
            elif column not in meta_tables.get(table, set()):
                errors.append(f"块 {block.block}.{where}: 列 {table}.{column} 不在 step1 meta 中")

    _check(f"{block.source_alias}.{block.user_col}", "SELECT 用户键锚定行")
    if block.join is not None:
        _check(block.join[3], "JOIN ON")
    if block.where:
        _check(block.where, "WHERE")
    for expression, name in block.feature_lines:
        _check(expression, f"特征 {name} 表达式")
        refs = set(_expression_references(substitute(expression)))
        declared = set(block_meta(block, name).source_columns)
        if refs != declared:
            missing = refs - declared
            extra = declared - refs
            if missing:
                errors.append(
                    f"块 {block.block} 特征 {name}: source_columns 未覆盖表达式引用 "
                    + ", ".join(f"{a}.{c}" for a, c in sorted(missing))
                )
            if extra:
                errors.append(
                    f"块 {block.block} 特征 {name}: source_columns 含表达式未引用的列 "
                    + ", ".join(f"{a}.{c}" for a, c in sorted(extra))
                )


def cross_check_refs(
    blocks: list[FeatureBlock],
    substitute_keep: _Substituter,
    meta_tables: dict[str, set[str]],
) -> None:
    """Run :func:`_check_refs` for every block; raise one ConfigError with all errors."""
    errors: list[str] = []
    for block in blocks:
        _check_refs(block, substitute_keep, meta_tables, errors)
    if errors:
        raise ConfigError("\n".join(f"- {item}" for item in errors))


def block_meta(block: FeatureBlock, name: str) -> BlockFeature:
    for feature in block.features:
        if feature.name == name:
            return feature
    raise KeyError(name)


def parse_config(
    config: dict[str, Any],
    meta_tables: dict[str, set[str]],
) -> tuple[list[FeatureBlock], list[str], float]:
    """Parse + validate everything. Returns ``(blocks, disabled_names, threshold)``."""
    errors: list[str] = []
    enabled = _enabled_set(config)

    profile = config.get("target_game_profile") or {}
    if not isinstance(profile, dict):
        raise ConfigError("target_game_profile 必须是对象")
    vocab = config.get("vocabularies") or {}
    if not isinstance(vocab, dict):
        raise ConfigError("vocabularies 必须是对象")
    for key, value in vocab.items():
        if isinstance(value, list):
            for item in value:
                _scalar(item, f"vocabularies.{key} 列表项")
        else:
            _scalar(value, f"vocabularies.{key}")

    threshold_raw = config.get("spearman_gate", DEFAULT_SPEARMAN_GATE)
    try:
        threshold = float(threshold_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"spearman_gate 必须是数值，实际 {threshold_raw!r}") from exc
    if not 0 < threshold <= 1:
        raise ConfigError(f"spearman_gate 必须在 (0, 1] 区间，实际 {threshold}")

    raw_blocks = config.get("feature_blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise ConfigError("feature_blocks 必须是非空列表")

    blocks: list[FeatureBlock] = []
    disabled: list[str] = []
    seen_blocks: set[str] = set()
    seen_features: dict[str, str] = {}
    all_feature_names: set[str] = set()
    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, dict):
            raise ConfigError(f"feature_blocks[{index}] 必须是对象")
        name = str(raw.get("block") or "").strip()
        if not IDENTIFIER_RE.fullmatch(name):
            raise ConfigError(f"feature_blocks[{index}].block={name!r} 不是安全标识符")
        if name in seen_blocks:
            raise ConfigError(f"块名重复：{name}")
        user_col = str(raw.get("user_col") or "").strip()
        if not IDENTIFIER_RE.fullmatch(user_col):
            raise ConfigError(f"块 {name} 的 user_col={user_col!r} 不是安全标识符")
        seen_blocks.add(name)
        try:
            features, block_disabled = _parse_feature_metadata(name, raw.get("features"), enabled)
            block_obj = FeatureBlock(
                block=name,
                user_col=user_col,
                source_table="",
                source_alias="",
                join=None,
                where=None,
                anchor_line="",
            )
            block_obj.features = features
            block_obj.declared_names = {feature.name for feature in features} | set(block_disabled)
            _parse_block_sql(block_obj, raw.get("sql"), enabled)
        except ConfigError as exc:
            errors.append(str(exc))
            continue
        for feature in block_obj.features:
            if feature.name in seen_features:
                errors.append(
                    f"特征 {feature.name} 在块 {seen_features[feature.name]} 与 {name} 中重复"
                )
            seen_features[feature.name] = name
        disabled.extend(block_disabled)
        all_feature_names.update(feature.name for feature in block_obj.features)
        all_feature_names.update(block_disabled)
        if block_obj.feature_lines:
            blocks.append(block_obj)
        else:
            disabled.extend([])  # fully-disabled block contributes nothing
    if enabled is not None:
        unknown = sorted(enabled - all_feature_names)
        if unknown:
            errors.append(
                "composite_features 引用了不存在的特征：" + ", ".join(unknown)
            )
    if not blocks:
        errors.append("没有任何启用的语义特征（全部被 composite_features 关闭或块解析失败）")

    if errors:
        raise ConfigError("\n".join(f"- {item}" for item in errors))
    return blocks, disabled, threshold


# ---------------------------------------------------------------------------
# placeholder substitution
# ---------------------------------------------------------------------------


class _Substituter:
    def __init__(
        self,
        source_db: str,
        target_game: str,
        profile: dict[str, Any],
        vocab: dict[str, Any],
        keep_target_game: bool,
    ) -> None:
        self.source_db = source_db
        self.target_game = target_game
        self.profile = profile
        self.vocab = vocab
        self.keep_target_game = keep_target_game

    def __call__(self, text: str) -> str:
        def param_repl(match: re.Match[str]) -> str:
            key = match.group(1)
            if key == "source_db":
                return self.source_db
            if key == "target_game":
                if self.keep_target_game:
                    return match.group(0)
                return _sql_literal(self.target_game)
            raise ConfigError(
                f"未知占位符 {match.group(0)}（只允许 source_db/target_game/profile.X/vocab.X）"
            )

        def profile_repl(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in self.profile:
                raise ConfigError(f"target_game_profile 缺少键 {key}（该特征已启用，必须补全）")
            return _sql_literal(_scalar(self.profile[key], f"target_game_profile.{key}"))

        def vocab_repl(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in self.vocab:
                raise ConfigError(f"vocabularies 缺少键 {key}（该特征已启用，必须补全）")
            value = self.vocab[key]
            if isinstance(value, list):
                if not value:
                    raise ConfigError(f"vocabularies.{key} 是空列表，无法渲染 IN (...)")
                return ", ".join(_sql_literal(item) for item in value)
            return _sql_literal(_scalar(value, f"vocabularies.{key}"))

        text = PARAMETER_RE.sub(param_repl, text)
        text = PROFILE_RE.sub(profile_repl, text)
        text = VOCAB_RE.sub(vocab_repl, text)
        return text


# ---------------------------------------------------------------------------
# artifact rendering
# ---------------------------------------------------------------------------


def _block_sql_lines(block: FeatureBlock, substitute: _Substituter) -> list[str]:
    """Render one block CTE verbatim from the config lines (placeholders replaced)."""
    lines = ["SELECT", f"    {substitute(block.anchor_line)}"]
    for expression, name in block.feature_lines:
        lines.append(f"    {substitute(expression)} AS {name},")
    lines[-1] = lines[-1].rstrip(",")
    lines.append(substitute(block.from_line))
    if block.join_line:
        lines.append(substitute(block.join_line))
    if block.where_line:
        lines.append(substitute(block.where_line))
    lines.append(substitute(block.group_line))
    return lines


def _render_create_sql(
    blocks: list[FeatureBlock],
    output_db: str,
    user_id: str,
    substitute: _Substituter,
    header: dict[str, Any],
) -> str:
    feature_count = sum(len(block.features) for block in blocks)
    lines: list[str] = [
        "-- step2_3b: 语义特征扩展（由 step2_3b_semantic_features.py 从语义配置生成）",
        f"-- 配置: {header['config_path']}（sha256 {header['config_sha256'][:12]}…）",
        f"-- 输入: {output_db}.{INPUT_TABLE}；输出: {output_db}.{SEMANTIC_TABLE}（用户键 {user_id}）",
        f"-- 装配形态：嵌套 1-JOIN-per-layer（每层恰好 1 个 LEFT JOIN，规避仓多 JOIN bug）；启用特征 {feature_count} 个。",
        "-- 本文件为生成器产物，禁止手工改写；发现问题请修正配置或参数后重新运行生成器。",
        "",
        f"CREATE OR REPLACE TABLE {output_db}.{SEMANTIC_TABLE}",
        "ENGINE = MergeTree",
        f"ORDER BY {user_id}",
        "SETTINGS allow_nullable_key = 1",
        "AS",
        "SELECT *",
        "FROM (",
    ]
    body: str = ""
    for index, block in enumerate(blocks, start=1):
        block_alias = f"b{index}"
        cols = ",\n        ".join(
            f"coalesce({block_alias}.{feature.name}, 0) AS {feature.name}"
            for feature in block.features
        )
        block_sql = "\n".join(
            "        " + line for line in _block_sql_lines(block, substitute)
        )
        if index == 1:
            body = (
                "    SELECT\n"
                "        w.*,\n"
                f"        {cols}\n"
                f"    FROM {output_db}.{INPUT_TABLE} AS w\n"
                "    LEFT JOIN (\n"
                f"{block_sql}\n"
                f"    ) AS {block_alias} ON w.{user_id} = {block_alias}.{block.user_col}"
            )
        else:
            previous = f"t{index - 1}"
            body = (
                "    SELECT\n"
                f"        {previous}.*,\n"
                f"        {cols}\n"
                "    FROM (\n"
                f"{body}\n"
                f"    ) AS {previous}\n"
                "    LEFT JOIN (\n"
                f"{block_sql}\n"
                f"    ) AS {block_alias} ON {previous}.{user_id} = {block_alias}.{block.user_col}"
            )
    lines.append(body)
    lines.append(");")
    return "\n".join(lines) + "\n"


def _render_validation_sql(
    feature_names: list[str],
    output_db: str,
    user_id: str,
    data_source: dict[str, Any],
) -> str:
    count = len(feature_names)
    names_sql = ", ".join(_sql_literal(name) for name in feature_names)
    text = f"""-- step2_3b: 语义宽表校验（单条 SELECT，全部布尔列须为 1）
-- 本文件由 step2_3b_semantic_features.py 生成；禁止手工改写，发现问题请修正配置后重新运行生成器。
-- <catalog_columns> 由生成器按 data_source.json 绑定，禁止手工替换。

SELECT
    count() AS n_rows,
    uniqExact({user_id}) AS n_users,
    (SELECT count() FROM {output_db}.{INPUT_TABLE}) AS base_rows,
    (SELECT count() FROM <catalog_columns>
     WHERE database = {_sql_literal(output_db)} AND table = '{INPUT_TABLE}') AS base_cols,
    (SELECT count() FROM <catalog_columns>
     WHERE database = {_sql_literal(output_db)} AND table = '{SEMANTIC_TABLE}') AS actual_cols,
    (SELECT count() FROM <catalog_columns>
     WHERE database = {_sql_literal(output_db)} AND table = '{SEMANTIC_TABLE}'
       AND name IN ({names_sql})) AS sem_cols,
    count() = base_rows AS row_count_preserved,
    count() = uniqExact({user_id}) AS user_key_unique,
    actual_cols = base_cols + {count} AS column_count_ok,
    sem_cols = {count} AS sem_columns_present
FORMAT JSON;
"""
    return bind_placeholders(text, data_source)


def _render_fragment(
    blocks: list[FeatureBlock],
    substitute_keep: _Substituter,
    context: dict[str, Any],
) -> dict[str, Any]:
    relation_plans: dict[str, Any] = {}
    features: dict[str, Any] = {}
    for block in blocks:
        joins: list[dict[str, str]] = []
        if block.join is not None:
            _, table, alias, on = block.join
            joins.append(
                {
                    # bare LEFT/INNER — matches the contract validator's ALLOWED_JOIN_TYPES
                    "type": block.join[0].split()[0],
                    "table": table,
                    "alias": alias,
                    "on": substitute_keep(on),
                }
            )
        filters = [substitute_keep(block.where)] if block.where else []
        relation_plans[block.block] = {
            "kind": "user_aggregation",
            "source": {"table": block.source_table, "alias": block.source_alias},
            "joins": joins,
            "filters": filters,
            "entity_key_expression": f"{block.source_alias}.{block.user_col}",
        }
        for expression, name in block.feature_lines:
            feature = block_meta(block, name)
            features[name] = {
                "relation_plan": block.block,
                "expression": substitute_keep(expression),
                "source_columns": [
                    {"alias": alias, "column": column} for alias, column in feature.source_columns
                ],
                "output_type": feature.dtype,
                # wide 表 CREATE 侧对每个 sem 列做 coalesce(x, 0)；契约 replay 必须同口径
                "null_policy": {"kind": "fill", "value": 0},
            }
    return {
        "contract_version": 1,
        "generated_by": "step2_3b_semantic_features.py",
        "semantic": context,
        "relation_plans": relation_plans,
        "features": features,
    }


def _render_semantic_md(
    blocks: list[FeatureBlock],
    disabled: list[str],
    header: dict[str, Any],
    threshold: float,
) -> str:
    feature_count = sum(len(block.features) for block in blocks)
    lines: list[str] = [
        "# step2_3b 语义先验特征（生成器产出，禁止手工改写）",
        "",
        f"- 配置：`{header['config_path']}`（sha256 `{header['config_sha256']}`）",
        f"- 目标游戏（任务参数原文）：{header['target_game']}",
        f"- 源库 / 输出库：{header['source_database']} / {header['output_database']}",
        f"- 输入 → 输出：`{INPUT_TABLE}` → `{SEMANTIC_TABLE}`（用户键 `{header['user_id']}`）",
        f"- 启用特征 {feature_count} 个，禁用 {len(disabled)} 个；Spearman 入选门槛 `{threshold}`",
        "",
        "块 = CTE 名 = 装配顺序 = 契约 relation_plan id。特征表达式与占位符替换结果见"
        f"`{SEMANTIC_SQL}`（所见即所得）。",
        "",
    ]
    for block in blocks:
        tables = block.source_table
        if block.join is not None:
            tables += f" JOIN {block.join[1]}"
        lines.append(f"## 块 `{block.block}`（源表 {tables}，用户键 `{block.user_col}`）")
        lines.append("")
        lines.append("| 特征 | 类型 | 语义 |")
        lines.append("|------|------|------|")
        for feature in block.features:
            description = feature.description.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{feature.name}` | {feature.dtype} | {description} |")
        lines.append("")
    if disabled:
        lines.append("## 默认/配置禁用的特征")
        lines.append("")
        lines.append(", ".join(f"`{name}`" for name in disabled))
        lines.append("")
    lines += [
        "## 产物与执行顺序",
        "",
        f"1. `{SEMANTIC_SQL}`（整条 CREATE，经 MCP `command_file` 原文提交）",
        f"2. `{VALIDATION_SQL}`（单条校验 SELECT，全部布尔列须为 1；collect 后 `save-collect` 为 `{RAW_VALIDATION}`）",
        "3. `step2_3b_semantic_features.py gate` → 写 `progress_step2_3b.txt`",
        f"4. step2_4 以 `{SEMANTIC_TABLE}` 为输入（驱动自动切换，见 SKILL.md）",
        "5. CSV 导出后：`step2_5_driver.py finalize`（自动串联 spearman-gate → merge-contract → validate-contract）",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# sub-command: generate / check
# ---------------------------------------------------------------------------


def _resolve_user_id(args: argparse.Namespace, out_dir: Path, meta_user_id_columns: list[str]) -> str:
    if args.user_id:
        return args.user_id
    schema_path = out_dir / SCHEMA
    if schema_path.is_file():
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
            value = str((schema or {}).get("user_id") or "").strip()
            if value:
                return value
        except (OSError, json.JSONDecodeError):
            pass
    if meta_user_id_columns:
        return meta_user_id_columns[0]
    raise SystemExit(
        "无法确定宽表用户键：请显式传 --user-id <user_id>（取 schema_resolution.json 的 user_id）"
    )


def _resolve_json_field(paths: list[Path], key: str) -> str:
    """First non-empty string ``key`` across the given JSON files (missing files ignored)."""
    for path in paths:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
    return ""


def _generate_or_check(args: argparse.Namespace, *, write: bool) -> int:
    out_dir: Path = args.out_dir
    config_path: Path = args.config
    if not config_path.is_file():
        raise SystemExit(f"未找到语义特征配置 {config_path}")
    if not args.meta.is_file():
        raise SystemExit(f"未找到 {args.meta}；step1 采样交接产物缺失")

    config = _load_config(config_path)
    source_db, meta_tables, meta_user_id_columns = _load_meta(args.meta)

    # 参数缺省时自动绑定权威来源（调用方不再需要考证/转述，少一层出错面）：
    # --output-db → schema_resolution.json 的 output_database（与整条 FE 流水线同源）；
    # --target-game → step1_sample_stats.json 的 target_game（采样阶段记录的任务参数，
    # 与 NL2SQL 渲染同源）。显式传参仍然优先：任务参数原文与上述来源不一致时，以
    # 原文为准、由调用方显式传参覆盖。
    output_db = str(args.output_db or "").strip() or _resolve_json_field(
        [out_dir / SCHEMA, args.meta.parent / SAMPLE_STATS], "output_database"
    )
    target_game = str(args.target_game or "").strip() or _resolve_json_field(
        [out_dir / SAMPLE_STATS, args.meta.parent / SAMPLE_STATS], "target_game"
    )
    if write and not output_db:
        raise SystemExit(
            "缺少 output_database：--output-db 未提供，schema_resolution.json 与 step1_sample_stats.json "
            "也无 output_database；请回查任务参数原文后显式传 --output-db，禁止从配置样例或仓清单猜测"
        )
    if write and not target_game:
        raise SystemExit(
            "缺少 target_game：--target-game 未提供，step1_sample_stats.json 也无 target_game；"
            "任务上下文缺失时停止并写阻塞原因，禁止从配置样例注释/描述或残留文件猜测"
        )

    if output_db and not IDENTIFIER_RE.fullmatch(output_db):
        raise SystemExit(
            f"--output-db 值 {output_db!r} 不是安全标识符（仅 [A-Za-z_][A-Za-z0-9_]）：来源为 --output-db、"
            "schema_resolution.json 的 output_database 或 step1_sample_stats.json；请核对来源值后重跑，"
            "禁止截断或猜测替换"
        )
    if not output_db:
        output_db = "<output_database>"
    if not target_game:
        target_game = "<target_game>"

    user_id = _resolve_user_id(args, out_dir, meta_user_id_columns)
    if not IDENTIFIER_RE.fullmatch(user_id):
        raise SystemExit(
            f"用户键 {user_id!r} 不是安全标识符（仅 [A-Za-z_][A-Za-z0-9_]）：来源为 --user-id、"
            "schema_resolution.json 的 user_id 或采样元数据 user_id_columns；请核对来源值后重跑"
        )

    try:
        blocks, disabled, threshold = parse_config(config, meta_tables)
        substitute_create = _Substituter(
            source_db, target_game, config.get("target_game_profile") or {},
            config.get("vocabularies") or {}, keep_target_game=False,
        )
        substitute_keep = _Substituter(
            source_db, target_game, config.get("target_game_profile") or {},
            config.get("vocabularies") or {}, keep_target_game=True,
        )
        # Ref cross-checks run on the substituted text — the exact text the
        # deployment-contract validator will later see.
        cross_check_refs(blocks, substitute_keep, meta_tables)
        # Render everything up front: this exercises every placeholder on every
        # enabled line (unknown profile/vocab keys raise here, not at submit time).
        feature_names = [name for block in blocks for _, name in block.feature_lines]
        header = {
            "config_path": str(config_path),
            "config_sha256": _sha256_text(config_path.read_text(encoding="utf-8-sig")),
            "target_game": target_game,
            "source_database": source_db,
            "output_database": output_db,
            "user_id": user_id,
        }
        create_sql = _render_create_sql(blocks, output_db, user_id, substitute_create, header)
        data_source = load_data_source(out_dir)
        validation_sql = _render_validation_sql(feature_names, output_db, user_id, data_source)
        context = {
            "stage": "step2_3b",
            "generator": "step2_3b_semantic_features.py",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config_path": str(config_path),
            "config_sha256": header["config_sha256"],
            "target_game_param": "{{target_game}}",
            "feature_count": len(feature_names),
        }
        fragment = _render_fragment(blocks, substitute_keep, context)
        semantic_md = _render_semantic_md(blocks, disabled, header, threshold)
    except ConfigError as exc:
        print("语义特征配置未通过校验：", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1

    if PARAMETER_RE.search(_strip_comments(create_sql)) or PARAMETER_RE.search(_strip_comments(validation_sql)):
        raise SystemExit("内部错误：生成的 SQL 仍残留 {{...}} 占位符")

    feature_count = len(feature_names)
    print(f"配置校验通过：{len(blocks)} 个特征块，启用 {feature_count} 个特征，禁用 {len(disabled)} 个")
    print(f"  源库 {source_db} → 输出库 {output_db}；输入 {INPUT_TABLE} → 输出 {SEMANTIC_TABLE}（用户键 {user_id}）")
    if not write:
        print("--check 完成：未写任何产物。")
        return 0

    apply_payload = {
        "step": "step2_3b",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "step2_3b_semantic_features.py",
        "config": {"path": str(config_path), "sha256": header["config_sha256"]},
        "source_database": source_db,
        "output_database": output_db,
        "target_game": target_game,
        "user_id": user_id,
        "input_table": INPUT_TABLE,
        "output_table": SEMANTIC_TABLE,
        "spearman_gate": threshold,
        "enabled_features": feature_names,
        "disabled_features": disabled,
        "blocks": [
            {
                "block": block.block,
                "plan_id": block.block,
                "user_col": block.user_col,
                "tables": [block.source_table] + ([block.join[1]] if block.join else []),
                "features": [feature.name for feature in block.features],
            }
            for block in blocks
        ],
        "artifacts": [SEMANTIC_SQL, VALIDATION_SQL, FRAGMENT_JSON, SEMANTIC_MD],
        "gate": None,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    # 剥掉 -- 注释行再写盘（与 step2_1/2_2/2_3 expanded SQL 同一约定）：
    # MCP 只接受 CREATE/SELECT 开头的语句，注释头会让整个文件被拒。
    _atomic_write_text(out_dir / SEMANTIC_SQL, _non_comment_text(create_sql).rstrip() + "\n")
    _atomic_write_text(out_dir / VALIDATION_SQL, _non_comment_text(validation_sql).rstrip() + "\n")
    _atomic_write_json(out_dir / FRAGMENT_JSON, fragment)
    _atomic_write_text(out_dir / SEMANTIC_MD, semantic_md)
    _atomic_write_json(out_dir / APPLY_JSON, apply_payload)

    leftovers = unbound_placeholders(validation_sql)
    if leftovers:
        print(
            f"警告：{VALIDATION_SQL} 仍有未绑定占位符 {leftovers}；工作区缺 data_source.json，"
            "已回退套件 references/data_source.json。",
            file=sys.stderr,
        )

    manifest = {
        "step": "step2_3b",
        "stage": "generate",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jobs": [
            {"name": SEMANTIC_TABLE, "command_file": SEMANTIC_SQL, "collect_as": None},
            {"name": "step2_3b_validation", "command_file": VALIDATION_SQL, "collect_as": RAW_VALIDATION},
        ],
        "after_collect": (
            f"{_CHECK} --manifest {MANIFEST} --expect-rows --out-dir {out_dir} && "
            f"python skill/feature-engineer/scripts/step2_3b_semantic_features.py gate "
            f"--raw-validation {RAW_VALIDATION} --out-dir {out_dir}"
        ),
        "save_note": (
            "collect_job 后必须用 step2_driver_common.py save-collect 落盘为清单 collect_as；"
            "禁止手工复制 payload。"
        ),
    }
    write_manifest(out_dir / MANIFEST, manifest)

    print(f"已写出 {SEMANTIC_SQL} / {VALIDATION_SQL} / {FRAGMENT_JSON} / {SEMANTIC_MD} / {APPLY_JSON}")
    print(f"已写出 {MANIFEST}：先提交建表（无 collect），再提交校验 SELECT 并落盘 {RAW_VALIDATION}，")
    print("然后执行 after_collect 的 gate 命令。")
    return 0


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))


# ---------------------------------------------------------------------------
# sub-command: gate (validation SELECT booleans)
# ---------------------------------------------------------------------------


def _payload_rows(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"未找到 step2_3b 校验 payload：{path}")
        for payload in _load_payloads(path):
            extracted = _extract_results(payload)
            if extracted:
                for columns, rows in extracted:
                    for row in rows:
                        if isinstance(row, dict):
                            records.append(dict(row))
                        elif isinstance(row, (list, tuple)):
                            records.append(
                                {
                                    str(columns[index]): (row[index] if index < len(row) else None)
                                    for index in range(len(columns))
                                }
                            )
                continue
            records.extend(_plain_records(payload))
    return records


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    return None


def _gate(args: argparse.Namespace) -> int:
    out_dir: Path = args.out_dir
    rows = _payload_rows(list(args.raw_validation))
    if not rows:
        raise SystemExit("step2_3b 校验结果为空，无法执行门禁")
    row = rows[0]
    n_rows = row.get("n_rows")
    try:
        n_rows_value = int(float(n_rows))
    except (TypeError, ValueError):
        n_rows_value = -1

    errors: list[str] = []
    if n_rows_value <= 0:
        errors.append(f"n_rows 必须为正整数，实际 {n_rows!r}")
    for key in ("row_count_preserved", "user_key_unique", "column_count_ok", "sem_columns_present"):
        if _as_bool(row.get(key)) is not True:
            errors.append(f"{key} 必须为 1/true，实际 {row.get(key)!r}")
    if errors:
        print("step2_3b 门禁未通过：", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        print(
            "\n修正配置或参数后重新运行生成器并重新提交；禁止手工创建 progress_step2_3b.txt 绕过门禁。",
            file=sys.stderr,
        )
        marker = args.marker or (out_dir / MARKER)
        if marker.exists():
            marker.unlink()
        return 1

    generated_at = datetime.now(timezone.utc).isoformat()
    marker = args.marker or (out_dir / MARKER)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        "PASSED: " + generated_at + f"（{n_rows_value} 行；{row.get('actual_cols')} 列 = "
        f"{row.get('base_cols')} 基础列 + {row.get('sem_cols')} 语义列）\n",
        encoding="utf-8",
    )
    print(f"step2_3b 门禁通过，已写出 {marker}")

    manifest_path = out_dir / MANIFEST
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and "after_collect" in payload:
            # 摘除 after_collect，避免 save-collect 守卫再次触发 gate；直接原子写，
            # 不走 write_manifest（其「N 个待提交 job」的摘要在 gate 之后有误导性）。
            payload.pop("after_collect", None)
            payload["stage"] = "gate"
            payload["generated_at"] = generated_at
            _atomic_write_json(manifest_path, payload)
    return 0


# ---------------------------------------------------------------------------
# sub-command: spearman-gate
# ---------------------------------------------------------------------------


def _average_ranks(values: list[float | None]) -> list[float | None]:
    """Average (tied) ranks over non-missing entries; None keeps None."""
    indexed = [(value, position) for position, value in enumerate(values) if value is not None]
    ranks: list[float | None] = [None] * len(values)
    if not indexed:
        return ranks
    indexed.sort(key=lambda item: item[0])
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][0] == indexed[i][0]:
            j += 1
        average = (i + j + 2) / 2.0  # 1-based ranks i+1 .. j+1
        for k in range(i, j + 1):
            ranks[indexed[k][1]] = average
        i = j + 1
    return ranks


def _pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 3:
        return None
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    xm = [value - mean_x for value in x]
    ym = [value - mean_y for value in y]
    denominator = math.sqrt(sum(v * v for v in xm) * sum(v * v for v in ym))
    if denominator <= 0:
        return None
    return sum(a * b for a, b in zip(xm, ym)) / denominator


def _parse_column(values: list[str]) -> tuple[list[float | None], bool]:
    """Return ``(parsed values, all_non_empty_parse_as_numeric)``."""
    parsed: list[float | None] = []
    numeric = True
    for value in values:
        if value is None or str(value).strip() == "":
            parsed.append(None)
            continue
        try:
            number = float(str(value).strip())
        except ValueError:
            numeric = False
            parsed.append(None)
            continue
        parsed.append(number if math.isfinite(number) else None)
    return parsed, numeric


def _exceeds(rho: float, threshold: float) -> bool:
    """1.0 阈值 = 仅剔除完全重复列（|ρ|=1）；其余阈值按「超过」严格比较。"""
    if threshold >= 1.0:
        return rho >= 1.0 - 1e-9
    return rho > threshold


def _spearman_gate(args: argparse.Namespace) -> int:
    out_dir: Path = args.out_dir
    csv_path: Path = args.csv or (out_dir / WIDE_CSV)
    apply_path = out_dir / APPLY_JSON
    fragment_path = out_dir / FRAGMENT_JSON

    apply_payload = _read_json(apply_path)
    if apply_payload.get("gate"):
        raise SystemExit(
            "spearman gate 已运行过（结果记录在 semantic_apply.json 的 gate 段）；禁止重复执行。"
            "如需重跑，请修正配置后从 generate 重新开始整个 step2_3b。"
        )
    if not (out_dir / MARKER).is_file():
        raise SystemExit(f"缺少 {MARKER}：先完成 step2_3b 建表、校验提交与 gate。")
    if not csv_path.is_file() or csv_path.stat().st_size == 0:
        raise SystemExit(f"未找到非空 {csv_path}；先完成 step2_4 CSV 导出再跑门槛。")

    threshold = float(args.threshold if args.threshold is not None else apply_payload.get("spearman_gate") or DEFAULT_SPEARMAN_GATE)
    user_id = str(apply_payload.get("user_id") or "").strip()
    label_column = ""
    schema_path = out_dir / SCHEMA
    if schema_path.is_file():
        try:
            label_column = str((json.loads(schema_path.read_text(encoding="utf-8-sig")) or {}).get("label") or "").strip()
        except (OSError, json.JSONDecodeError):
            label_column = ""
    sem_names = [str(name) for name in apply_payload.get("enabled_features") or []]
    if not sem_names:
        raise SystemExit("semantic_apply.json 缺少 enabled_features")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        header = next(reader, None)
        rows = [row for row in reader]
    if header is None or not rows:
        raise SystemExit(f"{csv_path} 缺少表头或数据行")

    missing_sem = [name for name in sem_names if name not in header]
    if missing_sem:
        raise SystemExit(
            f"CSV 缺少语义列：{', '.join(missing_sem)}（CSV 与 semantic_apply.json 不一致，"
            "请确认 step2_4 以 step2_3b_wide_semantic 为输入并重新导出）"
        )

    skip_columns = {name for name in (user_id, label_column) if name}
    column_values: list[list[str]] = [
        [row[index] if index < len(row) else "" for row in rows] for index in range(len(header))
    ]

    parsed: dict[str, list[float | None]] = {}
    numeric_old: list[str] = []
    for name, values in zip(header, column_values):
        if name in sem_names or name in skip_columns:
            continue
        numbers, is_numeric = _parse_column(values)
        distinct = {value for value in numbers if value is not None}
        if is_numeric and len(distinct) >= 2:
            parsed[name] = numbers
            numeric_old.append(name)

    sem_ranks: dict[str, list[float | None]] = {}
    for name in sem_names:
        values = column_values[header.index(name)]
        numbers, is_numeric = _parse_column(values)
        if not is_numeric:
            raise SystemExit(f"语义列 {name} 含无法解析为数值的非空单元格，CSV 疑似损坏")
        sem_ranks[name] = _average_ranks(numbers)
    old_ranks = {name: _average_ranks(parsed[name]) for name in numeric_old}

    details: list[dict[str, Any]] = []
    for name in sem_names:
        ranks_s = sem_ranks[name]
        distinct = {value for value in ranks_s if value is not None}
        max_rho: float | None = None
        partner = ""
        if len(distinct) >= 2:  # 常数列无法计算相关性，视为通过
            for old_name in numeric_old:
                ranks_o = old_ranks[old_name]
                x: list[float] = []
                y: list[float] = []
                for a, b in zip(ranks_s, ranks_o):
                    if a is not None and b is not None:
                        x.append(a)
                        y.append(b)
                rho = _pearson(x, y)
                if rho is None:
                    continue
                if max_rho is None or abs(rho) > abs(max_rho):
                    max_rho = rho
                    partner = old_name
        dropped_flag = max_rho is not None and _exceeds(abs(max_rho), threshold)
        details.append(
            {
                "feature": name,
                "max_rho": round(max_rho, 6) if max_rho is not None else None,
                "partner": partner,
                "kept": not dropped_flag,
            }
        )
    dropped = [item for item in details if not item["kept"]]
    kept = [item["feature"] for item in details if item["kept"]]

    dropped_names = {item["feature"] for item in dropped}
    if dropped_names:
        keep_indexes = [index for index, name in enumerate(header) if name not in dropped_names]
        temporary = csv_path.with_name(f".{csv_path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([header[index] for index in keep_indexes])
            for row in rows:
                writer.writerow([row[index] if index < len(row) else "" for index in keep_indexes])
        temporary.replace(csv_path)

        fragment = _read_json(fragment_path)
        fragment["features"] = {
            name: feature
            for name, feature in (fragment.get("features") or {}).items()
            if name not in dropped_names
        }
        referenced = {str(feature.get("relation_plan")) for feature in fragment["features"].values()}
        fragment["relation_plans"] = {
            plan_id: plan
            for plan_id, plan in (fragment.get("relation_plans") or {}).items()
            if plan_id in referenced
        }
        if isinstance(fragment.get("semantic"), dict):
            fragment["semantic"]["feature_count"] = len(fragment["features"])
            fragment["semantic"]["gated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(fragment_path, fragment)

    generated_at = datetime.now(timezone.utc).isoformat()
    apply_payload["gate"] = {
        "checked_at": generated_at,
        "threshold": threshold,
        "n_rows": len(rows),
        "old_numeric_columns": len(numeric_old),
        "kept": kept,
        "dropped": dropped,
        "csv_columns_before": len(header),
        "csv_columns_after": len(header) - len(dropped_names),
    }
    _atomic_write_json(apply_path, apply_payload)

    report = {
        "step": "step2_3b_spearman_gate",
        "generated_at": generated_at,
        "passed": True,
        "threshold": threshold,
        "csv": str(csv_path),
        "n_rows": len(rows),
        "old_numeric_columns": len(numeric_old),
        "sem_features": details,
        "kept": kept,
        "dropped": dropped,
    }
    _atomic_write_json(out_dir / GATE_REPORT_JSON, report)

    md_lines = [
        "# step2_3b Spearman 入选门槛报告",
        "",
        f"- 阈值：`{threshold}`（max|ρ| 超过即剔除；`1.0` = 仅剔除完全重复列）",
        f"- CSV：`{csv_path.name}`（{len(rows)} 行；门槛前 {len(header)} 列 → 门槛后 {len(header) - len(dropped_names)} 列）",
        f"- 参与比对的旧数值列：{len(numeric_old)} 个（排除 {user_id}/{label_column or 'label'}、字符串列与常数列）",
        f"- 结果：保留 {len(kept)} / 剔除 {len(dropped)}",
        "",
        "| 语义特征 | max\\|ρ\\| | 最强搭档老特征 | 结论 |",
        "|----------|----------|----------------|------|",
    ]
    for item in details:
        rho_text = "—" if item["max_rho"] is None else str(item["max_rho"])
        partner_text = f"`{item['partner']}`" if item["partner"] else "—"
        verdict = "保留" if item["kept"] else "剔除（与老特征重复）"
        md_lines.append(f"| `{item['feature']}` | {rho_text} | {partner_text} | {verdict} |")
    md_lines += [
        "",
        "门槛只判「是否与老特征重复」，不判「有没有用」（后者是 step3_2 IV 筛查的职责）；",
        "无法计算相关性的列（常数/样本不足）视为通过。门槛后的 CSV 即模型唯一训练数据。",
        "",
    ]
    _atomic_write_text(out_dir / GATE_REPORT_MD, "\n".join(md_lines))

    print(f"已写出 {GATE_REPORT_JSON} / {GATE_REPORT_MD}")
    print(f"保留 {len(kept)} 个语义特征，剔除 {len(dropped)} 个")
    for item in dropped:
        print(f"  剔除 {item['feature']}：max|ρ|={item['max_rho']}（vs {item['partner']}）")
    if not kept:
        print("全部语义特征被剔除：merge-contract 会跳过合并，CSV 不含语义列。")
    print(f"下一步：python skill/feature-engineer/scripts/step2_3b_semantic_features.py merge-contract --out-dir {out_dir}")
    return 0


# ---------------------------------------------------------------------------
# sub-command: merge-contract
# ---------------------------------------------------------------------------


def _merge_contract(args: argparse.Namespace) -> int:
    out_dir: Path = args.out_dir
    contract_path: Path = args.contract or (out_dir / CONTRACT)
    fragment_path = out_dir / FRAGMENT_JSON
    apply_path = out_dir / APPLY_JSON

    apply_payload = _read_json(apply_path)
    if not apply_payload.get("gate"):
        raise SystemExit(
            "semantic_apply.json 尚无 gate 段：请先跑 spearman-gate（CSV 导出后、"
            "契约合并前），再执行 merge-contract。"
        )

    fragment = _read_json(fragment_path)
    fragment_features = fragment.get("features") or {}
    if not fragment_features:
        print(
            f"{FRAGMENT_JSON} 不含任何特征（全部被门槛剔除或未启用）——跳过契约合并，"
            "契约保持不含语义特征。"
        )
        return 0

    contract = _read_json(contract_path)
    if contract.get("semantic_features"):
        raise SystemExit(
            f"{contract_path} 已合并过语义片段（存在 semantic_features 段）；禁止重复合并。"
            "如需重做，请从 generate 重新开始整个 step2_3b 并先恢复未合并的契约。"
        )
    plans = contract.get("relation_plans")
    features = contract.get("features")
    if not isinstance(plans, dict) or not isinstance(features, dict):
        raise SystemExit(f"{contract_path} 缺少 relation_plans/features 对象，无法合并")

    fragment_plans = fragment.get("relation_plans") or {}
    plan_collisions = sorted(set(fragment_plans) & set(plans))
    if plan_collisions:
        raise SystemExit("relation_plans id 与既有契约冲突：" + ", ".join(plan_collisions))
    feature_collisions = sorted(set(fragment_features) & set(features))
    if feature_collisions:
        raise SystemExit("features 名与既有契约冲突：" + ", ".join(feature_collisions))

    plans.update(fragment_plans)
    features.update(fragment_features)
    # The old structural_validation result no longer describes the merged contract;
    # drop it so registration requires a fresh validate-contract run (step2_5 finalize).
    contract.pop("validation", None)
    contract["semantic_features"] = {
        "merged_at": datetime.now(timezone.utc).isoformat(),
        "fragment": FRAGMENT_JSON,
        "generator": "step2_3b_semantic_features.py",
        "feature_count": len(fragment_features),
        "relation_plan_count": len(fragment_plans),
        "runtime_parameter": "{{target_game}}",
    }
    _atomic_write_json(contract_path, contract)
    print(f"已合并 {len(fragment_features)} 个语义特征（{len(fragment_plans)} 个 relation_plan）进 {contract_path}")
    print("已移除旧的 validation 段：合并后必须重跑校验器（step2_5_driver.py finalize 会自动执行）。")
    return 0


# ---------------------------------------------------------------------------
# public entries for drivers (step2_5 finalize auto-chain)
# ---------------------------------------------------------------------------


def run_spearman_gate(out_dir: Path, csv_path: Path | None = None, threshold: float | None = None) -> int:
    """Spearman 入选门槛的驱动入口（与 CLI ``spearman-gate`` 同一实现，单一来源）。"""
    return _spearman_gate(
        argparse.Namespace(out_dir=Path(out_dir), csv=csv_path, threshold=threshold)
    )


def run_merge_contract(out_dir: Path, contract: Path | None = None) -> int:
    """契约合并的驱动入口（与 CLI ``merge-contract`` 同一实现，单一来源）。"""
    return _merge_contract(argparse.Namespace(out_dir=Path(out_dir), contract=contract))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="step2_3b semantic prior features (config-driven, no warehouse access)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="validate config and render the 5 artifacts + manifest")
    generate.add_argument("--config", type=Path, required=True, help="semantic_feature_config YAML path")
    generate.add_argument("--meta", type=Path, default=Path("step1_output_meta.json"))
    generate.add_argument(
        "--output-db", type=str, default=None,
        help="default: schema_resolution.json output_database (explicit flag wins, e.g. verbatim task parameter)",
    )
    generate.add_argument(
        "--target-game", type=str, default=None,
        help="default: step1_sample_stats.json target_game (explicit flag wins, e.g. verbatim task parameter)",
    )
    generate.add_argument("--user-id", type=str, default=None, help="wide-table user key (default: schema_resolution.user_id)")
    generate.add_argument("--out-dir", type=Path, default=Path("."))
    generate.set_defaults(func=lambda args: _generate_or_check(args, write=True))

    check = sub.add_parser("check", help="config health check only; writes nothing")
    check.add_argument("--config", type=Path, required=True)
    check.add_argument("--meta", type=Path, default=Path("step1_output_meta.json"))
    check.add_argument("--output-db", type=str, default=None)
    check.add_argument("--target-game", type=str, default=None)
    check.add_argument("--user-id", type=str, default=None)
    check.add_argument("--out-dir", type=Path, default=Path("."))
    check.set_defaults(func=lambda args: _generate_or_check(args, write=False))

    gate = sub.add_parser("gate", help="decide the step2_3b validation booleans and write the marker")
    gate.add_argument("--raw-validation", type=Path, action="append", required=True, help="validation payload; repeatable")
    gate.add_argument("--out-dir", type=Path, default=Path("."))
    gate.add_argument("--marker", type=Path, default=None)
    gate.set_defaults(func=_gate)

    spearman = sub.add_parser("spearman-gate", help="drop sem_ columns duplicating old numeric features (post CSV export)")
    spearman.add_argument("--out-dir", type=Path, default=Path("."))
    spearman.add_argument("--csv", type=Path, default=None, help="default: <out-dir>/step2_4_wide_userfiltered.csv")
    spearman.add_argument("--threshold", type=float, default=None, help="default: spearman_gate from semantic_apply.json")
    spearman.set_defaults(func=_spearman_gate)

    merge = sub.add_parser("merge-contract", help="merge the gated contract fragment into the deployment contract")
    merge.add_argument("--contract", type=Path, default=None, help="default: <out-dir>/step2_2_deployment_feature_contract.json")
    merge.add_argument("--out-dir", type=Path, default=Path("."))
    merge.set_defaults(func=_merge_contract)

    args = parser.parse_args()
    raise SystemExit(int(args.func(args) or 0))


if __name__ == "__main__":
    main()
