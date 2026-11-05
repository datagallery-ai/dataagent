"""Emit a warehouse SQL CASE expression that maps a geo column to {col}_tier.

Column-name patterns come from ``references/scene.yaml`` ``geo_columns``. Do not
hardcode scene column names in Skill text or SQL templates.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from functools import lru_cache
from pathlib import Path

_DEFAULT_MAP = (
    Path(__file__).resolve().parent.parent / "references" / "city_tier_map.json"
)
_SCENE_YAML = (
    Path(__file__).resolve().parent.parent / "references" / "scene.yaml"
)
_WHITESPACE_RE = re.compile(r"[\s\u3000]+")
_DEFAULT_CITY_SUFFIXES = ("特别行政区", "自治州", "地区", "盟", "市")
_DEFAULT_COUNTY_SUFFIXES = ("自治县", "县")

TIER_ORDER = ("一线", "新一线", "二线", "三线")


@lru_cache(maxsize=1)
def _geo_columns() -> dict:
    """Load geo column patterns from references/scene.yaml. Fallback keeps prior behavior."""
    defaults = {
        "name_patterns": ["city", "city_name", "reside_city", "user_city"],
        "name_contains": ["城市"],
        "exclude_patterns": ["cityhash", "hash_city"],
        "output_suffix": "_tier",
    }
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(_SCENE_YAML.read_text(encoding="utf-8")) or {}
        geo = raw.get("geo_columns") or {}
    except Exception:
        return defaults
    merged = dict(defaults)
    for key in ("name_patterns", "name_contains", "exclude_patterns"):
        values = geo.get(key)
        if isinstance(values, list) and values:
            merged[key] = [str(item).strip() for item in values if str(item).strip()]
    suffix = geo.get("output_suffix")
    if isinstance(suffix, str) and suffix.strip():
        merged["output_suffix"] = suffix.strip()
    return merged


def _geo_name_re() -> re.Pattern[str]:
    geo = _geo_columns()
    tokens = [re.escape(p) for p in geo["name_patterns"]]
    contains = [re.escape(p) for p in geo["name_contains"]]
    pattern = rf"(?i)(^|_)({'|'.join(tokens)})(_|$)"
    if contains:
        pattern = rf"{pattern}|{'|'.join(contains)}"
    return re.compile(pattern)


def _geo_exclude_re() -> re.Pattern[str]:
    tokens = [re.escape(p) for p in _geo_columns()["exclude_patterns"]]
    return re.compile(rf"(?i){'|'.join(tokens)}")


def geo_column_match_sql() -> str:
    """Warehouse boolean SQL that matches raw geo columns (for validation templates)."""
    geo = _geo_columns()
    tokens = "|".join(re.escape(p) for p in geo["name_patterns"])
    excludes = "|".join(re.escape(p) for p in geo["exclude_patterns"])
    parts = [f"match(name, '(?i)(^|_)({tokens})(_|$)')"]
    for item in geo["name_contains"]:
        escaped = str(item).replace("'", "''")
        parts.append(f"position(name, '{escaped}') > 0")
    inner = " OR ".join(parts)
    return f"NOT match(name, '(?i){excludes}')\n  AND ({inner})"


def is_city_column(name: str) -> bool:
    """Return True when *name* looks like a geo field, not a hash helper."""
    if _geo_exclude_re().search(name or ""):
        return False
    return bool(_geo_name_re().search(name or ""))


def is_raw_city_column(name: str) -> bool:
    """True for original city columns that must not appear in the wide table."""
    if (name or "").endswith("_tier"):
        return False
    return is_city_column(name)


def raw_city_columns(names: list[str]) -> list[str]:
    """Return city original columns that would overfit if left in the wide table."""
    return [name for name in names if is_raw_city_column(name)]


def _sql_string_list(values: list[str]) -> str:
    escaped = []
    for item in values:
        text = str(item).strip()
        if not text:
            continue
        escaped.append("'" + text.replace("'", "''") + "'")
    return ", ".join(escaped)


def _suffixes(mapping: dict, key: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = mapping.get(key)
    if not isinstance(raw, list) or not raw:
        return defaults
    values = [str(item).strip() for item in raw if str(item).strip()]
    return tuple(values) or defaults


def _compact_text(value: str) -> str:
    return _WHITESPACE_RE.sub("", (value or "").strip())


def _strip_city_suffix(text: str, city_suffixes: tuple[str, ...]) -> str:
    for suffix in city_suffixes:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def _compact_expr(qualified_column: str) -> str:
    inner = f"trimBoth(toString({qualified_column}))"
    return f"replaceAll(replaceAll({inner}, ' ', ''), '　', '')"


def _norm_expr(compact_expr: str, city_suffixes: tuple[str, ...]) -> str:
    pattern = "|".join(city_suffixes)
    return f"replaceRegexpAll({compact_expr}, '({pattern})$', '')"


def _county_expr(compact_expr: str, county_suffixes: tuple[str, ...]) -> str:
    pattern = "|".join(county_suffixes)
    return f"match({compact_expr}, '({pattern})$')"


def _city_variants(name: str) -> list[str]:
    variants = [name]
    if not name.endswith("市"):
        variants.append(name + "市")
    return variants


def normalize_city_name(value: str, mapping: dict | None = None) -> str:
    """Strip city-level suffixes so 北京市 / 北京 市 match 北京. Do not strip 县."""
    mapping = mapping or {}
    compact = _compact_text(value)
    return _strip_city_suffix(
        compact, _suffixes(mapping, "city_suffixes", _DEFAULT_CITY_SUFFIXES)
    )


def build_city_tier_sql(qualified_column: str, output_alias: str, mapping: dict) -> str:
    """Return CAST(multiIf(...)) AS {output_alias} using *mapping*."""
    tiers: dict[str, list[str]] = mapping.get("tiers") or {}
    unknown_values = list(mapping.get("unknown_values") or [])
    unknown_tier = str(mapping.get("unknown_tier") or "未知")
    unmapped_tier = str(mapping.get("unmapped_tier") or "三线及以下")
    city_suffixes = _suffixes(mapping, "city_suffixes", _DEFAULT_CITY_SUFFIXES)
    county_suffixes = _suffixes(mapping, "county_suffixes", _DEFAULT_COUNTY_SUFFIXES)
    compact = _compact_expr(qualified_column)
    norm = _norm_expr(compact, city_suffixes)
    branches: list[str] = [
        f"{qualified_column} IS NULL OR trimBoth(toString({qualified_column})) = '', '{unknown_tier}'",
        f"{_county_expr(compact, county_suffixes)}, '{unmapped_tier}'",
    ]
    if unknown_values:
        unknown_match = list(dict.fromkeys(
            _compact_text(str(item)) for item in unknown_values if str(item).strip()
        ))
        unknown_match.extend(
            normalize_city_name(item, mapping) for item in list(unknown_match)
        )
        unknown_match = list(dict.fromkeys(item for item in unknown_match if item))
        branches.append(
            f"{compact} IN ({_sql_string_list(unknown_match)})"
            f" OR {norm} IN ({_sql_string_list(unknown_match)}), '{unknown_tier}'"
        )
    seen: set[str] = set()
    for tier in TIER_ORDER:
        bare: list[str] = []
        variants: list[str] = []
        variant_seen: set[str] = set()
        for city in tiers.get(tier) or []:
            name = str(city).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            bare.append(name)
            for variant in _city_variants(name):
                if variant not in variant_seen:
                    variant_seen.add(variant)
                    variants.append(variant)
        if not bare:
            continue
        branches.append(
            f"{norm} IN ({_sql_string_list(bare)})"
            f" OR {compact} IN ({_sql_string_list(variants)}), '{tier}'"
        )
    branches.append(f"'{unmapped_tier}'")
    body = ",\n        ".join(branches)
    return (
        f"CAST(\n"
        f"        multiIf(\n"
        f"        {body}\n"
        f"        ) AS LowCardinality(String)\n"
        f"    ) AS {output_alias}"
    )


def _load_mapping(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _csv_header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            return [str(item).strip() for item in next(reader)]
        except StopIteration:
            return []


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate {col}_tier warehouse SQL from the bundled geo map"
    )
    parser.add_argument("--column", help="unqualified geographic column name")
    parser.add_argument("--table-alias", default="w", help="FROM alias, default w")
    parser.add_argument("--map", type=Path, default=_DEFAULT_MAP)
    parser.add_argument(
        "--geo-match-sql",
        action="store_true",
        help="print warehouse SQL that matches raw geo columns (expand validation templates)",
    )
    parser.add_argument(
        "--check-csv",
        type=Path,
        help="fail if the CSV header still contains a raw geographic column",
    )
    args = parser.parse_args()

    if args.geo_match_sql:
        print(geo_column_match_sql())
        return

    mapping = _load_mapping(args.map)

    if args.check_csv is not None:
        leaked = raw_city_columns(_csv_header(args.check_csv))
        if leaked:
            raise SystemExit(
                "raw geographic column leaked into wide table (overfitting): "
                + ", ".join(leaked)
                + "; keep only {col}_tier"
            )
        return

    if not args.column:
        raise SystemExit(
            "--column is required unless --check-csv / --geo-match-sql is set"
        )
    if not is_city_column(args.column):
        raise SystemExit(f"{args.column!r} does not look like a geographic column")
    alias = f"{args.table_alias}." if args.table_alias else ""
    qualified = f"{alias}{args.column}"
    print(build_city_tier_sql(qualified, f"{args.column}_tier", mapping))


if __name__ == "__main__":
    main()
