#!/usr/bin/env python3
"""Load user_sampling/config.yaml — the only scene-specific vocabulary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROLE_KEYS = (
    "user_table",
    "label_event",
    "activity_event",
    "conversion_event",
    "game_dim",
)
COLUMN_FAMILIES = ("user_id", "game", "label", "event_time")
SCHEMA_ALIAS_KEYS = {
    "user_id": "user_id_columns",
    "game": "game_columns",
    "label": "label_columns",
    "event_time": "event_time_columns",
}


class SceneConfigError(ValueError):
    """config.yaml is missing, unreadable, or internally inconsistent."""


@dataclass(frozen=True)
class ColumnFamilies:
    user_id: tuple[str, ...]
    game: tuple[str, ...]
    label: tuple[str, ...]
    event_time: tuple[str, ...]

    def family(self, name: str) -> tuple[str, ...]:
        try:
            return getattr(self, name)
        except AttributeError as exc:
            raise SceneConfigError(f"unknown column family {name!r}") from exc


@dataclass(frozen=True)
class RoleRule:
    name: str
    pick: str
    limit: int
    tables: tuple[str, ...]
    match: tuple[str, ...]
    match_exact: tuple[str, ...]
    match_suffix: tuple[str, ...]
    match_prefix: tuple[str, ...]
    require_columns: tuple[str, ...]
    prefer_columns: tuple[str, ...]
    forbid_columns: tuple[str, ...]
    prefer_match: tuple[str, ...]
    prefer_prefix: tuple[str, ...]
    demote_match: tuple[str, ...]
    exclude_roles: tuple[str, ...]


@dataclass(frozen=True)
class SceneConfig:
    path: Path
    version: int
    sha256: str
    columns: ColumnFamilies
    roles: dict[str, RoleRule]
    catalog_tables: tuple[str, ...]
    catalog_match: tuple[str, ...]

    def schema_metadata(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "file": self.path.name,
        }

    def assert_schema_compatible(self, schema: Mapping[str, Any]) -> None:
        metadata = schema.get("scene_config")
        if not isinstance(metadata, dict):
            return
        schema_hash = str(metadata.get("sha256") or "")
        if schema_hash and schema_hash != self.sha256:
            raise SceneConfigError(
                "step1_0_table_schema.json was generated from a different scene config; "
                "rerun build_table_schema.py"
            )

    def aliases_for(self, schema: Mapping[str, Any] | None, family: str) -> tuple[str, ...]:
        """Prefer aliases already written into table_schema.json."""
        fallback = self.columns.family(family)
        if not isinstance(schema, Mapping):
            return fallback
        aliases = schema.get("column_aliases")
        if not isinstance(aliases, dict):
            return fallback
        values = aliases.get(SCHEMA_ALIAS_KEYS[family])
        if not isinstance(values, list):
            return fallback
        found = tuple(str(item).strip() for item in values if str(item).strip())
        return found or fallback

    def is_catalog_copy(self, table: str) -> bool:
        lowered = str(table or "").strip().lower()
        if not lowered:
            return False
        if lowered in {name.lower() for name in self.catalog_tables}:
            return True
        parts = tuple(part for part in lowered.split("_") if part)
        for token in self.catalog_match:
            normalized = token.lower().strip("_")
            if not normalized:
                continue
            token_parts = tuple(part for part in normalized.split("_") if part)
            if len(token_parts) == 1 and normalized in parts:
                return True
            if token_parts and any(
                parts[index : index + len(token_parts)] == token_parts
                for index in range(len(parts) - len(token_parts) + 1)
            ):
                return True
        return False

    def role_candidates(self, tables: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
        indexed = _index_tables(tables)
        names = [item["name"] for item in tables if str(item.get("name") or "").strip()]
        assigned: dict[str, list[str]] = {key: [] for key in ROLE_KEYS}
        taken: dict[str, set[str]] = {}
        for key in ROLE_KEYS:
            rule = self.roles.get(key)
            if rule is None:
                continue
            excluded = set()
            for other in rule.exclude_roles:
                excluded.update(taken.get(other, ()))
            picked = self._pick_role(rule, names, indexed, excluded)
            assigned[key] = picked
            taken[key] = set(picked)
        if not assigned["user_table"]:
            assigned["user_table"] = _fallback_user_table(names, indexed, self.columns.user_id)
        if not assigned["game_dim"]:
            user_set = set(assigned["user_table"])
            assigned["game_dim"] = _fallback_game_dim(
                names, indexed, user_set, self.columns.game, self.columns.user_id
            )
        return assigned

    def column_aliases(self, tables: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
        realized = {
            SCHEMA_ALIAS_KEYS[family]: _realized_aliases(tables, self.columns.family(family))
            for family in COLUMN_FAMILIES
        }
        if not realized["user_id_columns"]:
            realized["user_id_columns"] = list(self.columns.user_id)
        return realized

    def _pick_role(
        self,
        rule: RoleRule,
        names: list[str],
        indexed: dict[str, set[str]],
        excluded: set[str],
    ) -> list[str]:
        eligible = [
            name
            for name in names
            if name not in excluded and self._columns_ok(rule, indexed.get(name, set()))
        ]
        exact = {item.lower() for item in rule.tables}
        pinned = [name for name in eligible if name.lower() in exact]
        fuzzy = [name for name in eligible if _name_hits(name, rule)]
        if rule.pick == "first" and pinned:
            pool = pinned
        elif pinned:
            seen: set[str] = set()
            pool = []
            for name in pinned + fuzzy:
                if name not in seen:
                    seen.add(name)
                    pool.append(name)
        else:
            pool = fuzzy
        pool.sort(key=lambda name: _role_sort_key(name, rule, indexed.get(name, set()), self.columns))
        if rule.pick == "first":
            return pool[:1]
        return pool[: rule.limit] if rule.limit > 0 else pool

    def _columns_ok(self, rule: RoleRule, cols: set[str]) -> bool:
        for family in rule.require_columns:
            if not _has_alias(cols, self.columns.family(family)):
                return False
        for family in rule.forbid_columns:
            if _has_alias(cols, self.columns.family(family)):
                return False
        return True


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.yaml"


def add_config_argument(parser: Any) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Scene config YAML (default: skill/user_sampling/config.yaml)",
    )


def load_scene_config(path: Path | None = None) -> SceneConfig:
    config_path = Path(path) if path else default_config_path()
    try:
        import yaml
    except ImportError as exc:
        raise SceneConfigError("PyYAML is required to read user_sampling/config.yaml") from exc
    try:
        config_text = config_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(config_text)
    except OSError as exc:
        raise SceneConfigError(f"cannot read {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SceneConfigError(f"invalid YAML {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SceneConfigError(f"{config_path} must be a mapping")
    try:
        version = int(raw.get("version") or 1)
    except (TypeError, ValueError) as exc:
        raise SceneConfigError(f"{config_path} version must be an integer") from exc
    if version != 1:
        raise SceneConfigError(f"{config_path} version={version} is not supported")
    columns_raw = raw.get("columns")
    if not isinstance(columns_raw, dict):
        raise SceneConfigError(f"{config_path} columns: is required")
    columns = ColumnFamilies(
        user_id=_str_tuple(columns_raw.get("user_id"), "columns.user_id"),
        game=_str_tuple(columns_raw.get("game"), "columns.game"),
        label=_str_tuple(columns_raw.get("label"), "columns.label"),
        event_time=_str_tuple(columns_raw.get("event_time"), "columns.event_time"),
    )
    if not columns.user_id:
        raise SceneConfigError(f"{config_path} columns.user_id must be a non-empty list")
    if not columns.game:
        raise SceneConfigError(f"{config_path} columns.game must be a non-empty list")
    roles_raw = raw.get("roles") if isinstance(raw.get("roles"), dict) else {}
    roles = {key: _parse_role(key, roles_raw.get(key)) for key in ROLE_KEYS}
    for role in roles.values():
        for family in role.require_columns + role.prefer_columns + role.forbid_columns:
            if family not in COLUMN_FAMILIES:
                raise SceneConfigError(
                    f"roles.{role.name} references unknown column family {family!r}"
                )
        for excluded_role in role.exclude_roles:
            if excluded_role not in ROLE_KEYS:
                raise SceneConfigError(
                    f"roles.{role.name}.exclude_roles references unknown role {excluded_role!r}"
                )
    projection = raw.get("projection") if isinstance(raw.get("projection"), dict) else {}
    catalog = projection.get("catalog_copy") if isinstance(projection.get("catalog_copy"), dict) else {}
    return SceneConfig(
        path=config_path,
        version=version,
        sha256=hashlib.sha256(config_text.encode("utf-8")).hexdigest(),
        columns=columns,
        roles=roles,
        catalog_tables=_str_tuple(catalog.get("tables"), "projection.catalog_copy.tables"),
        catalog_match=_str_tuple(catalog.get("match"), "projection.catalog_copy.match"),
    )


def _parse_role(name: str, raw: Any) -> RoleRule:
    data = raw if isinstance(raw, dict) else {}
    default_pick = "first" if name in {"user_table", "game_dim"} else "all"
    default_limit = 1 if default_pick == "first" else 3
    pick = str(data.get("pick") or default_pick).strip() or default_pick
    if pick not in {"first", "all"}:
        raise SceneConfigError(f"roles.{name}.pick must be first or all")
    try:
        limit = int(data["limit"]) if data.get("limit") is not None else default_limit
    except (TypeError, ValueError) as exc:
        raise SceneConfigError(f"roles.{name}.limit must be an int") from exc
    return RoleRule(
        name=name,
        pick=pick,
        limit=limit,
        tables=_str_tuple(data.get("tables"), f"roles.{name}.tables"),
        match=_str_tuple(data.get("match"), f"roles.{name}.match"),
        match_exact=_str_tuple(data.get("match_exact"), f"roles.{name}.match_exact"),
        match_suffix=_str_tuple(data.get("match_suffix"), f"roles.{name}.match_suffix"),
        match_prefix=_str_tuple(data.get("match_prefix"), f"roles.{name}.match_prefix"),
        require_columns=_str_tuple(data.get("require_columns"), f"roles.{name}.require_columns"),
        prefer_columns=_str_tuple(data.get("prefer_columns"), f"roles.{name}.prefer_columns"),
        forbid_columns=_str_tuple(data.get("forbid_columns"), f"roles.{name}.forbid_columns"),
        prefer_match=_str_tuple(data.get("prefer_match"), f"roles.{name}.prefer_match"),
        prefer_prefix=_str_tuple(data.get("prefer_prefix"), f"roles.{name}.prefer_prefix"),
        demote_match=_str_tuple(data.get("demote_match"), f"roles.{name}.demote_match"),
        exclude_roles=_str_tuple(data.get("exclude_roles"), f"roles.{name}.exclude_roles"),
    )


def _str_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise SceneConfigError(f"{field} must be a string or list of strings")


def _index_tables(tables: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    indexed: dict[str, set[str]] = {}
    for table in tables:
        name = str(table.get("name") or "").strip()
        if not name:
            continue
        cols = {
            str(col.get("name") or "").strip().lower()
            for col in (table.get("columns") or [])
            if isinstance(col, dict) and str(col.get("name") or "").strip()
        }
        indexed[name] = cols
    return indexed


def _realized_aliases(tables: Sequence[Mapping[str, Any]], aliases: tuple[str, ...]) -> list[str]:
    actual: dict[str, str] = {}
    for table in tables:
        for col in table.get("columns") or []:
            if not isinstance(col, dict):
                continue
            name = str(col.get("name") or "").strip()
            if name and name.lower() not in actual:
                actual[name.lower()] = name
    found: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        key = alias.lower()
        if key in actual and key not in seen:
            seen.add(key)
            found.append(actual[key])
    return found


def _has_alias(cols: set[str], aliases: Iterable[str]) -> bool:
    return any(alias.lower() in cols for alias in aliases)


def _name_hits(table: str, rule: RoleRule) -> bool:
    constrained = bool(
        rule.tables or rule.match or rule.match_exact or rule.match_suffix or rule.match_prefix
    )
    if not constrained:
        return True
    lowered = table.lower()
    if lowered in {item.lower() for item in rule.tables}:
        return True
    if lowered in {item.lower() for item in rule.match_exact}:
        return True
    if any(token.lower() in lowered for token in rule.match):
        return True
    for suffix in rule.match_suffix:
        token = suffix.lower()
        if lowered == token or lowered.endswith(f"_{token}") or lowered.endswith(token):
            return True
    return any(lowered.startswith(token.lower()) for token in rule.match_prefix)


def _role_sort_key(
    name: str,
    rule: RoleRule,
    cols: set[str],
    columns: ColumnFamilies,
) -> tuple[Any, ...]:
    lowered = name.lower()
    exact = lowered in {item.lower() for item in rule.tables}
    match_exact = lowered in {item.lower() for item in rule.match_exact}
    prefer_match = any(token.lower() in lowered for token in rule.prefer_match)
    prefer_prefix = any(lowered.startswith(token.lower()) for token in rule.prefer_prefix)
    prefer_cols = all(_has_alias(cols, columns.family(family)) for family in rule.prefer_columns) if rule.prefer_columns else False
    demote = any(token.lower() in lowered for token in rule.demote_match)
    return (
        0 if exact else 1,
        0 if match_exact else 1,
        0 if prefer_match else 1,
        0 if prefer_prefix else 1,
        0 if prefer_cols else 1,
        0 if not demote else 1,
        lowered,
    )


def _fallback_user_table(
    names: list[str],
    indexed: dict[str, set[str]],
    user_id: tuple[str, ...],
) -> list[str]:
    for name in names:
        if _has_alias(indexed.get(name, set()), user_id):
            return [name]
    return []


def _fallback_game_dim(
    names: list[str],
    indexed: dict[str, set[str]],
    user_set: set[str],
    game: tuple[str, ...],
    user_id: tuple[str, ...],
) -> list[str]:
    for name in names:
        if name in user_set:
            continue
        cols = indexed.get(name, set())
        if _has_alias(cols, user_id):
            continue
        if _has_alias(cols, game):
            return [name]
    return []
