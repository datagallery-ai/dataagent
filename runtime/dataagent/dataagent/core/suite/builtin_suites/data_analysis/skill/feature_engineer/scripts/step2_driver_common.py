#!/usr/bin/env python3
"""Shared helpers for the step2 driver scripts.

The drivers turn "Agent remembers a script sequence, hand-binds warehouse placeholders
and invents local file names" into "run one command, read the run manifest".

Two facilities live here:

  * ``bind_placeholders`` -- replace the warehouse placeholders that were intentionally
    left for submit time (``<catalog_columns>`` / ``<catalog_tables>``) using the
    machine-readable ``data_source.json`` materialized into the workspace by step1_0.
    The replacement follows the same rule every expander already uses: only
    non-comment lines are rewritten, so template documentation stays intact.
  * ``write_manifest`` -- emit ``step2_x_run.json`` describing the exact submit order,
    the command file, the local collect filename and the follow-up commands.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Workspace-relative first (step1_0 may copy it there), then the packaged skill
# copy under ``references/``. That copy is the same file and keeps step2 runnable
# when the upstream sampling stage does not materialize a workspace copy.
_PACKAGED_DATA_SOURCE = (
    Path(__file__).resolve().parent.parent / "references" / "data_source.json"
)
_DATA_SOURCE_CANDIDATES = (
    Path("data_source.json"),
    _PACKAGED_DATA_SOURCE,
)

_PLACEHOLDER_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>")

# Only these warehouse placeholders are bound at drive time. Everything else
# ({{output_database}}, <user_id>, ...) is bound by the individual expander.
_BINDABLE = ("catalog_columns", "catalog_tables")


def find_data_source(start: Path | None = None) -> Path | None:
    """Return the workspace ``data_source.json`` path, or ``None`` when absent."""
    base = start or Path(".")
    for candidate in _DATA_SOURCE_CANDIDATES:
        path = (base / candidate) if not candidate.is_absolute() else candidate
        if path.is_file():
            return path
    return None


def load_data_source(start: Path | None = None) -> dict[str, Any]:
    """Load ``data_source.json``; return ``{}`` when it is not materialized yet."""
    path = find_data_source(start)
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def bind_placeholders(sql_text: str, data_source: dict[str, Any]) -> str:
    """Bind ``<catalog_columns>`` / ``<catalog_tables>`` on non-comment lines only."""
    replacements = {key: str(data_source[key]).strip() for key in _BINDABLE if data_source.get(key)}
    if not replacements:
        return sql_text

    lines: list[str] = []
    for line in sql_text.splitlines():
        if line.lstrip().startswith("--"):
            lines.append(line)
            continue

        def _sub(match: re.Match[str]) -> str:
            return replacements.get(match.group(1), match.group(0))

        lines.append(_PLACEHOLDER_RE.sub(_sub, line))
    return "\n".join(lines)


def unbound_placeholders(sql_text: str) -> list[str]:
    """Return bindable placeholders still present on non-comment lines."""
    found: list[str] = []
    for line in sql_text.splitlines():
        if line.lstrip().startswith("--"):
            continue
        for match in _PLACEHOLDER_RE.finditer(line):
            if match.group(1) in _BINDABLE and match.group(1) not in found:
                found.append(match.group(1))
    return found


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    """Write a run manifest and print a short human summary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    jobs = payload.get("jobs") or []
    print(f"已写出 {path}：{len(jobs)} 个待提交 job")
    for index, job in enumerate(jobs, start=1):
        collect = job.get("collect_as") or "-"
        print(f"  {index}. {job.get('name')}  ->  {collect}")


def run_script(path: Path, argv: list[str]) -> int:
    """Run a sibling gate script's ``main()`` with *argv*, returning its exit code.

    Keeps each gate algorithm single-sourced: the driver never re-implements it.
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载脚本 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    previous = sys.argv
    sys.argv = [str(path), *argv]
    try:
        module.main()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    finally:
        sys.argv = previous
    return 0


# ---------------------------------------------------------------------------
# collect → save atomic helpers (save-collect / check-collect CLI)
# ---------------------------------------------------------------------------

_MARKER_SUFFIX = ".via-save-collect"
_STALE_AGE_SECONDS = 20 * 60
_TOOL_RESULTS_DIR = Path("tool-results")
_LINE_NUMBER_PREFIX_RE = re.compile(r"^\d+\t")
_SCRIPT_CLI = "python skill/feature-engineer/scripts/step2_driver_common.py"


def _strip_line_number_prefixes(text: str) -> str:
    """Strip ``N\\t`` line-number prefixes that tool-results dumps may carry."""
    lines: list[str] = []
    for line in text.splitlines():
        lines.append(_LINE_NUMBER_PREFIX_RE.sub("", line, count=1))
    return "\n".join(lines)


def _columns_from_row_dicts(rows: list[Any]) -> list[str] | None:
    if not rows or not all(isinstance(item, dict) for item in rows):
        return None
    first = rows[0]
    assert isinstance(first, dict)
    return [str(key) for key in first.keys()]


def _is_columns_rows_block(item: Any) -> bool:
    """True when *item* is a dict carrying both ``columns`` and ``rows`` keys."""
    return isinstance(item, dict) and "columns" in item and "rows" in item


def _first_columns_rows(obj: Any) -> tuple[list[str] | None, list[Any] | None]:
    """Recursively extract the first ``(columns, rows)`` pair from a collect payload.

    Also accepts a bare list of row dicts (columns inferred from the first row's keys)
    and a dict that only carries a ``rows`` list of dicts.

    Multi-block payloads ``[{columns, rows}, …]`` are treated as block-shaped: never
    mistaken for a flat rows list (whose first element must not be a columns+rows dict).
    """
    if isinstance(obj, dict):
        columns = obj.get("columns")
        rows = obj.get("rows")
        if isinstance(rows, list):
            if isinstance(columns, list):
                return [str(col) for col in columns], rows
            inferred = _columns_from_row_dicts(rows)
            if inferred is not None:
                return inferred, rows
        for key in ("outputs", "data", "result", "payload", "original_msg", "content"):
            if key in obj:
                found = _first_columns_rows(obj[key])
                if found != (None, None):
                    return found
        return None, None

    if isinstance(obj, list):
        # Block-shaped: list of {columns, rows} envelopes — walk items, do not flatten.
        if obj and _is_columns_rows_block(obj[0]):
            for item in obj:
                found = _first_columns_rows(item)
                if found != (None, None):
                    return found
            return None, None
        # Flat rows = non-empty list of dicts whose first element is NOT a columns+rows block.
        if obj and all(isinstance(item, dict) for item in obj) and not _is_columns_rows_block(obj[0]):
            inferred = _columns_from_row_dicts(obj)
            if inferred is not None:
                return inferred, obj
        for item in obj:
            found = _first_columns_rows(item)
            if found != (None, None):
                return found
    return None, None


def _marker_path(out: Path) -> Path:
    """Return the sidecar marker path ``<out>.via-save-collect``."""
    return Path(str(out) + _MARKER_SUFFIX)


def _read_marker_source(marker: Path) -> str | None:
    try:
        text = marker.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Plain-text markers store the source path alone.
        return text.splitlines()[0].strip() or None
    if isinstance(payload, dict):
        source = payload.get("source")
        return str(source).strip() if source else None
    if isinstance(payload, str):
        return payload.strip() or None
    return None


def _consumed_sources(out_dir: Path | None = None) -> set[str]:
    """Scan ``*.via-save-collect`` markers and return the recorded source path set."""
    roots: list[Path] = []
    if out_dir is not None:
        roots.append(Path(out_dir))
    roots.append(Path("."))
    if _TOOL_RESULTS_DIR.is_dir():
        roots.append(_TOOL_RESULTS_DIR)

    seen: set[str] = set()
    sources: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            markers = list(root.rglob(f"*{_MARKER_SUFFIX}"))
        except OSError:
            continue
        for marker in markers:
            key = str(marker.resolve()) if marker.exists() else str(marker)
            if key in seen:
                continue
            seen.add(key)
            source = _read_marker_source(marker)
            if source:
                sources.add(source)
                try:
                    sources.add(str(Path(source).resolve()))
                except OSError:
                    pass
    return sources


def _looks_like_collect_payload(text_or_obj: str | Any) -> bool:
    """Return True when *text_or_obj* looks like a warehouse collect result."""
    obj: Any
    if isinstance(text_or_obj, str):
        text = _strip_line_number_prefixes(text_or_obj).strip()
        if not text:
            return False
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return False
    else:
        obj = text_or_obj

    columns, rows = _first_columns_rows(obj)
    if columns is not None and rows is not None:
        return True
    # Flat rows list (explicitly not a multi-block [{columns,rows}, …] payload).
    if (
        isinstance(obj, list)
        and obj
        and all(isinstance(item, dict) for item in obj)
        and not _is_columns_rows_block(obj[0])
    ):
        return True
    if isinstance(obj, dict):
        rows_only = obj.get("rows")
        if isinstance(rows_only, list):
            return True
    return False


def _collect_as_names(manifest: dict[str, Any]) -> list[str]:
    """Gather non-empty ``collect_as`` filenames from jobs and top-level collect."""
    names: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        name = str(value or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for job in manifest.get("jobs") or []:
        if isinstance(job, dict):
            _add(job.get("collect_as"))

    collect = manifest.get("collect")
    if isinstance(collect, dict):
        _add(collect.get("collect_as"))
    elif isinstance(collect, list):
        for item in collect:
            if isinstance(item, dict):
                _add(item.get("collect_as"))
    return names


def _remedy_command(out_name: str, *, extract_rows: bool = False) -> str:
    cmd = f"{_SCRIPT_CLI} save-collect --source <tool-results/id.txt> --out {out_name}"
    if extract_rows:
        cmd += " [--extract-rows]"
    return cmd


def check_collect(
    manifest: Path | str | dict[str, Any],
    *,
    expect_rows: bool = False,
    out_dir: Path = Path("."),
) -> int:
    """Guard that every ``collect_as`` was saved via ``save-collect``.

    Returns 0 on success, 1 when any target is missing, unmarked, or (with
    ``expect_rows``) has an empty/absent rows list.
    """
    if isinstance(manifest, dict):
        payload = manifest
        manifest_label = "<manifest>"
    else:
        path = Path(manifest)
        manifest_label = str(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"check-collect: 无法读取 manifest {path}：{exc}", file=sys.stderr)
            return 1
        if not isinstance(payload, dict):
            print(f"check-collect: {path} 必须是 JSON 对象", file=sys.stderr)
            return 1

    names = _collect_as_names(payload)
    if not names:
        print(f"check-collect: {manifest_label} 未声明任何 collect_as，跳过")
        return 0

    base = Path(out_dir)
    missing: list[str] = []
    unmarked: list[str] = []
    empty_rows: list[str] = []
    bad_json: list[str] = []

    for name in names:
        target = base / name if not Path(name).is_absolute() else Path(name)
        if not target.is_file() or target.stat().st_size == 0:
            missing.append(name)
            continue
        marker = _marker_path(target)
        if not marker.is_file():
            unmarked.append(name)
            continue
        if not expect_rows:
            continue
        try:
            raw = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            bad_json.append(name)
            continue
        _columns, rows = _first_columns_rows(raw)
        if not isinstance(rows, list) or len(rows) == 0:
            empty_rows.append(name)

    if not (missing or unmarked or empty_rows or bad_json):
        print(f"check-collect: ok（{len(names)} 个 collect_as）")
        return 0

    print("check-collect: 失败 — collect→save 原子化未满足：", file=sys.stderr)
    if missing:
        print(f"  缺失/空文件: {', '.join(missing)}", file=sys.stderr)
    if unmarked:
        print(
            f"  缺少 {_MARKER_SUFFIX} 标记（疑似手工内联写盘）: {', '.join(unmarked)}",
            file=sys.stderr,
        )
    if bad_json:
        print(f"  JSON 无法解析: {', '.join(bad_json)}", file=sys.stderr)
    if empty_rows:
        print(
            f"  expect_rows 但 rows 为空/缺失（可能误存了 CREATE 的 0 行响应）: "
            f"{', '.join(empty_rows)}",
            file=sys.stderr,
        )
    print("补救：用 save-collect 重新落盘，例如：", file=sys.stderr)
    for name in names:
        if name in missing or name in unmarked or name in empty_rows or name in bad_json:
            print(f"  {_remedy_command(name, extract_rows=expect_rows)}", file=sys.stderr)
    return 1


def _write_marker(out: Path, source: Path) -> None:
    marker = _marker_path(out)
    payload = {
        "source": str(source),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _resolve_latest_source(*, out_dir: Path) -> Path | None:
    """Pick the newest unconsumed collect-like file under ``tool-results/``."""
    root = _TOOL_RESULTS_DIR
    if not root.is_dir():
        return None
    consumed = _consumed_sources(out_dir)
    candidates: list[Path] = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".txt", ".json"}:
            continue
        if path.name.endswith(_MARKER_SUFFIX):
            continue
        resolved = str(path.resolve())
        if str(path) in consumed or resolved in consumed:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if not _looks_like_collect_payload(text):
            continue
        candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[0]


def save_collect(
    source: Path | str | None,
    out: Path | str,
    *,
    extract_rows: bool = False,
    source_latest: bool = False,
) -> int:
    """Persist a collect payload to *out* and write the ``.via-save-collect`` marker.

    Foolproof path: readable file → strip line numbers → JSON validate → write out +
    marker. Explicit ``--source`` is never refused for “illegitimacy”; only missing
    files / invalid JSON fail.
    """
    out_path = Path(out)
    out_dir = out_path.parent if out_path.parent != Path("") else Path(".")

    if source_latest:
        latest = _resolve_latest_source(out_dir=out_dir)
        if latest is None:
            print(
                "save-collect --latest: 在 tool-results/ 下找不到未消费且结构像 collect 结果的 "
                ".txt/.json。\n"
                "小结果常以内联形式返回、不会落 tool-results/；"
                "请先把 payload 写入临时文件后用 --source <path>。",
                file=sys.stderr,
            )
            return 1
        age = time.time() - latest.stat().st_mtime
        if age > _STALE_AGE_SECONDS:
            print(
                f"警告：选中的 {latest} 已超过 {_STALE_AGE_SECONDS // 60} 分钟"
                f"（{int(age)}s），可能不是本次 collect 的结果。",
                file=sys.stderr,
            )
        source_path = latest
        print(f"save-collect --latest: 选用 {source_path}")
    else:
        if source is None:
            print("save-collect: 需要 --source 或 --latest", file=sys.stderr)
            return 1
        source_path = Path(source)
        if not source_path.is_file():
            print(f"save-collect: 未找到 source {source_path}", file=sys.stderr)
            return 1

    try:
        text = source_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        print(f"save-collect: 读取 {source_path} 失败：{exc}", file=sys.stderr)
        return 1

    stripped = _strip_line_number_prefixes(text).strip()
    if not stripped:
        print(f"save-collect: {source_path} 为空", file=sys.stderr)
        return 1
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        print(f"save-collect: {source_path} 不是合法 JSON：{exc}", file=sys.stderr)
        return 1

    _columns, rows = _first_columns_rows(payload)
    row_count = len(rows) if isinstance(rows, list) else 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if extract_rows:
        if rows is None:
            print(
                f"save-collect: --extract-rows 但未能从 {source_path} 提取 rows",
                file=sys.stderr,
            )
            return 1
        out_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        # Keep the original payload shape; never force-wrap into {columns, rows}.
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    _write_marker(out_path, source_path)

    print(f"rows={row_count}, out={out_path}")
    if row_count == 0:
        print("注意：0 行落盘，不是失败，请勿重复提交。")
    return 0


def save_collect_batch(
    *,
    sources: list[Path | str] | None = None,
    source_glob: str | None = None,
    name_prefix: str,
    start_index: int = 0,
    extract_rows: bool = False,
    out_dir: Path = Path("."),
) -> int:
    """Batch-save collect payloads as ``{name_prefix}{start_index+i}.json``.

    ``--source-glob`` / explicit ``--sources`` skip files already recorded in
    ``.via-save-collect`` markers (yellow-style consume filter only).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    consumed = _consumed_sources(out_dir)
    source_paths: list[Path] = []
    if sources:
        for item in sources:
            path = Path(item)
            if not path.is_file():
                print(f"save-collect --batch: 未找到 source {path}", file=sys.stderr)
                return 1
            if str(path) in consumed or str(path.resolve()) in consumed:
                continue
            source_paths.append(path)
    elif source_glob:
        # Resolve relative to cwd; support ``tool-results/*.txt`` style globs.
        matched = sorted(Path().glob(source_glob), key=lambda item: item.stat().st_mtime)
        for path in matched:
            if not path.is_file():
                continue
            if str(path) in consumed or str(path.resolve()) in consumed:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            if not _looks_like_collect_payload(text):
                continue
            source_paths.append(path)
    else:
        print("save-collect --batch: 需要 --sources 或 --source-glob", file=sys.stderr)
        return 1

    if not source_paths:
        print("save-collect --batch: 没有可落盘的 source", file=sys.stderr)
        return 1

    failed = 0
    for offset, source_path in enumerate(source_paths):
        out_name = f"{name_prefix}{start_index + offset}.json"
        code = save_collect(
            source_path,
            out_dir / out_name,
            extract_rows=extract_rows,
            source_latest=False,
        )
        if code != 0:
            failed += 1
    if failed:
        print(f"save-collect --batch: {failed}/{len(source_paths)} 失败", file=sys.stderr)
        return 1
    print(f"save-collect --batch: 完成 {len(source_paths)} 个 -> {name_prefix}{start_index}+")
    return 0


def _cmd_save_collect(args: argparse.Namespace) -> int:
    if args.batch:
        if not args.name_prefix:
            print("save-collect --batch: 需要 --name-prefix", file=sys.stderr)
            return 1
        sources = list(args.sources) if args.sources else None
        return save_collect_batch(
            sources=sources,
            source_glob=args.source_glob,
            name_prefix=args.name_prefix,
            start_index=args.start_index,
            extract_rows=args.extract_rows,
            out_dir=args.out_dir if args.out_dir is not None else Path("."),
        )

    if bool(args.source) == bool(args.latest):
        # XOR: exactly one of --source / --latest
        if args.source and args.latest:
            print("save-collect: --source 与 --latest 互斥", file=sys.stderr)
        else:
            print("save-collect: 需要 --source 或 --latest", file=sys.stderr)
        return 1
    if args.out is None:
        print("save-collect: 需要 --out", file=sys.stderr)
        return 1
    return save_collect(
        args.source,
        args.out,
        extract_rows=args.extract_rows,
        source_latest=bool(args.latest),
    )


def _cmd_check_collect(args: argparse.Namespace) -> int:
    return check_collect(args.manifest, expect_rows=args.expect_rows, out_dir=args.out_dir)


def main() -> int:
    """CLI entry for ``save-collect`` / ``check-collect``."""
    parser = argparse.ArgumentParser(
        description="step2 collect→save atomic helpers (save-collect / check-collect)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    save = sub.add_parser("save-collect", help="persist a collect payload with a source marker")
    source_group = save.add_mutually_exclusive_group()
    source_group.add_argument("--source", type=Path, default=None, help="tool-results/<id>.txt or JSON")
    source_group.add_argument(
        "--latest",
        action="store_true",
        help="pick the newest unconsumed collect-like file under tool-results/",
    )
    save.add_argument("--out", type=Path, default=None, help="destination collect_as path")
    save.add_argument(
        "--extract-rows",
        action="store_true",
        help="write only the rows list (flat list of row dicts / arrays)",
    )
    save.add_argument("--batch", action="store_true", help="batch mode: multiple sources -> prefixed files")
    save.add_argument("--sources", type=Path, nargs="*", default=None, help="explicit source list for --batch")
    save.add_argument("--source-glob", type=str, default=None, help="glob of sources for --batch (mtime order)")
    save.add_argument("--name-prefix", type=str, default=None, help="output name prefix for --batch")
    save.add_argument("--start-index", type=int, default=0, help="first index for --batch names")
    save.add_argument("--out-dir", type=Path, default=Path("."), help="output directory for --batch")
    save.set_defaults(func=_cmd_save_collect)

    check = sub.add_parser("check-collect", help="verify collect_as files exist with save markers")
    check.add_argument("--manifest", type=Path, required=True, help="step2_x_run.json")
    check.add_argument(
        "--expect-rows",
        action="store_true",
        help="require a non-empty rows list inside each collect_as JSON",
    )
    check.add_argument("--out-dir", type=Path, default=Path("."), help="directory holding collect_as files")
    check.set_defaults(func=_cmd_check_collect)

    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())

