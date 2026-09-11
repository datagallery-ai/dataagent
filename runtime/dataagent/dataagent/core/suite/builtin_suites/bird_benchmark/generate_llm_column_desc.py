#!/usr/bin/env python3
"""LLM 列描述生成器 — 复用 step1b prompt + CSV 语料，生成 column_description_short (D12).

独立脚本（不入主仓运行时流程），产出 JSON cache 供 bird_to_osi_yaml.py --llm-desc-cache 使用。
不降级为 CSV 描述：对每列调 LLM 生成 2-6 词英文语义短语。

LLM 调用复用主仓 ``LLMClient``，脚本只负责 BIRD 数据 I/O 和 JSON 契约。

用法:
  # 生成 LLM 列描述 cache
  python -m dataagent.core.suite.builtin_suites.bird_benchmark.generate_llm_column_desc \\
    --db-id california_schools \\
    --bird-dir nl2sql/data/dev/dev_databases \\
    --output /tmp/llm_desc_california_schools.json

  # 然后传给 bird_to_osi_yaml.py
  python -m dataagent.core.suite.builtin_suites.bird_benchmark.bird_to_osi_yaml \\
    --db-id california_schools \\
    --llm-desc-cache /tmp/llm_desc_california_schools.json \\
    --output /tmp/california_schools_osi.yaml

LLM 配置（与 nl2sql preprocess config 对齐）:
  环境变量: LLM_API_BASE / LLM_MODEL / LLM_API_KEY
  CLI 覆盖: --api-base / --model（密钥仅从环境变量读取）
  默认 provider: deepseek_v4_flash (https://api.deepseek.com/v1)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from dataagent.core.managers.llm_manager.llm_client import LLMClient
from dataagent.core.suite.builtin_suites.bird_benchmark.model_options import normalize_api_base, normalize_extra_body

logger = logging.getLogger("generate_llm_column_desc")


def _normalize_api_base(value: str) -> str:
    """Accept either an OpenAI-compatible root URL or a full chat endpoint."""
    return normalize_api_base(value)


def _clean_json_response(content: str) -> str:
    """Remove common reasoning and Markdown wrappers before JSON parsing."""
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    content = content.strip()
    if content.startswith("```"):
        _, separator, content = content.partition("\n")
        if not separator:
            return ""
    if content.rstrip().endswith("```"):
        content = content.rstrip().rsplit("```", 1)[0]
    return content.strip()


def request_json(
    llm_client: LLMClient,
    prompt: str,
    *,
    temperature: float,
    max_attempts: int,
    retry_delay: float,
    backoff_multiplier: float,
) -> dict[str, Any]:
    """Call the shared client with one total retry budget for transport and JSON errors."""
    delay = retry_delay
    for attempt in range(1, max_attempts + 1):
        try:
            response = llm_client.invoke(
                [{"role": "user", "content": prompt}],
                temperature=temperature,
                num_retries=0,
            )
            parsed = json.loads(_clean_json_response(response.content))
            if not isinstance(parsed, dict):
                raise ValueError("LLM response is not a JSON object")
            return parsed
        except Exception as exc:
            logger.warning(
                "LLM/JSON attempt %d/%d failed: %s%s",
                attempt,
                max_attempts,
                type(exc).__name__,
                " (retrying...)" if attempt < max_attempts else " (giving up)",
            )
            if attempt < max_attempts:
                time.sleep(delay)
                delay *= backoff_multiplier
    return {}


# ── step1b LLM prompt（复制自 nl2sql/preprocess/builders/step1b_column_desc.py）──

_COLUMN_DESC_PROMPT = """You are a professional data catalog assistant.

Input:
Notes on input fields (you can trust them):
- original_column_name: the column id that exactly matches the real database schema.
- csv_column_name: a human-curated alias for the column (may be empty).
- csv_column_description: a human-curated column description (may be empty).

Inputs (use ONLY these inputs):
- Original column name (schema-accurate column id): {original_column_name}
- CSV column name (may be empty): {csv_column_name}
- CSV column description (may be empty): {csv_column_description}

Context:
The output field will be stored as column_description_short and used for hybrid retrieval:
- vector similarity search (semantic)

Task:
Generate EXACTLY ONE short English semantic phrase for retrieval, strictly matching the rules below.

Required output pattern (single phrase):
- Semantic-only: describe what the values represent.
- Output MUST be a compact noun phrase (typically 2-6 words, maximum 10 words).
- Do NOT mention the word "column".
- Do NOT use boilerplate prefixes like "Identifier for ...", "The ... stores ...", or "The ... contains ...".
- Do NOT include relationship/mapping phrases like "mapping to ...", "maps to ...", "foreign key", "primary key".
- STRICT input priority:
  - First, rely on csv_column_name + csv_column_description.
  - If BOTH are empty, rely ONLY on original_column_name; do NOT infer additional meaning.
- Do NOT guess or invent business meaning.
- Do NOT include any "N-character" / length statements.

Output requirements:
1. Output MUST be valid JSON.
2. Do NOT output any extra text.
3. Do NOT wrap with markdown code fences.

Output JSON schema:
{{
  "simple_description": "..."
}}

Detailed rules:
A. Style and structure
- EXACTLY 1 phrase.
- Must be <= 10 words.
- Must be a single-line string (no newline characters).
- Prefer a noun phrase; avoid boilerplate verbs like "stores"/"contains".
- Do NOT include table/database/qualified names.

B. Content inclusion rules (match the example)
- Include: what the values represent (business semantics), but only when directly supported by CSV inputs.
- If csv_column_name/csv_column_description are empty, use original_column_name tokens conservatively, e.g.:
  - original_column_name="account_id" -> "account identifier"

C. Content exclusion rules
- Do NOT include any numeric statistics or sample values.
- Do NOT include length/character-count statements.
- Do NOT output markdown.

D. Length and retrieval focus
- Keep it compact and keyword-rich; prefer stable nouns (table/column/entity names) over verbs.
- If meaning is unclear, be conservative and generic (e.g., "record identifier", "categorical code") and avoid guessing business meaning.

Example (for OUTPUT FORMAT only; do NOT copy any token from this example into your answer; treat it as schema illustration, not as semantic guidance):
Input:
- Original column name (schema-accurate column id): order_total_amount
- CSV column name (may be empty): TotalAmount
- CSV column description (may be empty): total amount of the order in cents

Example output:
{{
  "simple_description": "order total amount"
}}
"""


# ── helper functions（复用 bird_to_osi_yaml.py 读取逻辑）──────────────────


def load_table_info(db_path: Path, table: str) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"PRAGMA table_info(`{table}`);").fetchall()
    return [
        {
            "cid": r[0],
            "name": r[1],
            "type": r[2],
            "notnull": r[3],
            "default": r[4],
            "pk": r[5],
        }
        for r in rows
    ]


def load_desc_map(bird_dir: Path, db_id: str, table: str) -> dict[str, dict[str, str]]:
    csv_path = bird_dir / db_id / "database_description" / f"{table}.csv"
    if not csv_path.exists():
        return {}
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with csv_path.open("r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f, skipinitialspace=True)
                out: dict[str, dict[str, str]] = {}
                for row in reader:
                    orig = (row.get("original_column_name") or "").strip()
                    if not orig:
                        continue
                    out[orig] = {
                        "column_name": (row.get("column_name") or "").strip(),
                        "column_description": (row.get("column_description") or "").strip(),
                        "value_description": (row.get("value_description") or "").strip(),
                    }
                return out
        except UnicodeDecodeError:
            continue
    return {}


# ── 主流程 ──────────────────────────────────────────────────────────────────


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(description="LLM 列描述生成器 (D12) — 复用 step1b prompt + CSV 语料")
    ap.add_argument("--db-id", required=True, help="BIRD database id (e.g. california_schools)")
    ap.add_argument(
        "--bird-dir",
        default="nl2sql/data/dev/dev_databases",
        help="BIRD dev_databases root directory",
    )
    ap.add_argument(
        "--output",
        required=True,
        help="Output JSON cache path (key=table.column → column_description_short)",
    )
    # LLM 配置
    ap.add_argument(
        "--api-base",
        default=os.environ.get("LLM_API_BASE", "https://api.deepseek.com/v1"),
        help="LLM API base URL (env: LLM_API_BASE)",
    )
    ap.add_argument(
        "--model",
        default=os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
        help="LLM model name (env: LLM_MODEL)",
    )
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument(
        "--max-attempts",
        type=int,
        default=int(os.environ.get("LLM_MAX_ATTEMPTS", os.environ.get("LLM_MAX_RETRIES", "3"))),
        help=(
            "Total attempts shared by transport and JSON failures "
            "(env: LLM_MAX_ATTEMPTS; LLM_MAX_RETRIES is a compatibility fallback)"
        ),
    )
    ap.add_argument(
        "--connect-timeout",
        type=float,
        default=float(os.environ.get("LLM_CONNECT_TIMEOUT", "10")),
    )
    ap.add_argument(
        "--read-timeout",
        type=float,
        default=float(os.environ.get("LLM_READ_TIMEOUT", "60")),
    )
    ap.add_argument(
        "--enable-thinking",
        nargs="?",
        const="true",
        choices=("true", "false", "omit"),
        default="omit",
        help="Gateway enable_thinking field; omitted by default; a bare flag means true",
    )
    ap.add_argument("--thinking", choices=("enabled", "disabled", "omit"), default="omit")
    ap.add_argument("--reasoning-effort", default=None)
    ap.add_argument("--extra-body", type=json.loads, default=None, help="Additional JSON request fields")
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="Unsupported legacy flag; configure the shared outbound TLS policy instead",
    )
    a = ap.parse_args()
    if a.max_attempts < 1:
        ap.error("--max-attempts must be at least 1")
    if a.insecure:
        ap.error(
            "--insecure is unsupported by the current LLMClient; configure the shared outbound TLS "
            "policy using DATAAGENT_OUTBOUND_SSL_SERVICES, DATAAGENT_OUTBOUND_MODE and "
            "DATAAGENT_OUTBOUND_CA_FILE as appropriate"
        )
    try:
        api_base = _normalize_api_base(a.api_base)
        extra_body = normalize_extra_body(
            thinking=a.thinking,
            enable_thinking=a.enable_thinking,
            reasoning_effort=a.reasoning_effort,
            extra_body=a.extra_body,
        )
    except ValueError as exc:
        ap.error(str(exc))

    bird_dir = Path(a.bird_dir).resolve()
    db_path = bird_dir / a.db_id / f"{a.db_id}.sqlite"
    if not db_path.exists():
        logger.error("sqlite not found: %s", db_path)
        sys.exit(1)

    # 读取 sqlite 实际表
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence';"
            ).fetchall()
        ]
    logger.info("db=%s tables=%s", a.db_id, tables)

    # 加载已有 cache（续跑）
    out_path = Path(a.output)
    cache: dict[str, str] = {}
    if out_path.exists():
        try:
            cache = json.loads(out_path.read_text(encoding="utf-8"))
            logger.info("loaded existing cache: %d entries", len(cache))
        except Exception:
            logger.warning("failed to load existing cache, starting fresh")

    # 复用主仓客户端；重试由 request_json 统一控制，避免嵌套放大。
    timeout = httpx.Timeout(
        connect=a.connect_timeout,
        read=a.read_timeout,
        write=a.read_timeout,
        pool=a.connect_timeout,
    )
    llm_client = LLMClient(
        api_base=api_base,
        model=a.model,
        api_key=os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", ""),
        timeout=timeout,
        num_retries=0,
        extra_body=extra_body,
    )
    logger.info(
        "LLMClient: model=%s base=%s attempts=%d timeout=(%s,%s)",
        a.model,
        api_base,
        a.max_attempts,
        a.connect_timeout,
        a.read_timeout,
    )

    total = 0
    generated = 0
    skipped = 0
    failed = 0

    for tbl in tables:
        cols = load_table_info(db_path, tbl)
        desc_map = load_desc_map(bird_dir, a.db_id, tbl)
        for c in cols:
            cname = c["name"]
            total += 1
            cache_key = f"{tbl}.{cname}"

            # 续跑：已有 LLM 描述 → 跳过
            if cache_key in cache and cache[cache_key]:
                skipped += 1
                continue

            desc_row = desc_map.get(cname, {})
            csv_column_name = (desc_row.get("column_name") or "").strip()
            csv_column_description = (desc_row.get("column_description") or "").strip()

            prompt = _COLUMN_DESC_PROMPT.format(
                original_column_name=cname or "UNKNOWN",
                csv_column_name=csv_column_name or "EMPTY",
                csv_column_description=csv_column_description or "EMPTY",
            )
            parsed = request_json(
                llm_client,
                prompt,
                temperature=a.temperature,
                max_attempts=a.max_attempts,
                retry_delay=float(os.environ.get("LLM_RETRY_DELAY", "2")),
                backoff_multiplier=float(os.environ.get("LLM_BACKOFF_MULTIPLIER", "2")),
            )
            desc_short = str(parsed.get("simple_description") or parsed.get("column_description_short") or "").strip()

            if not desc_short:
                failed += 1
                logger.error("LLM failed for %s; leaving it uncached for retry", cache_key)
                continue

            generated += 1

            cache[cache_key] = desc_short
            logger.info("[%s] %s → %s", cache_key, cname, desc_short)

            # 每 10 列 flush 一次（增量保存）
            if (generated + failed) % 10 == 0:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info("flushed cache: %d entries", len(cache))

    # 最终 flush
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(
        "done: db=%s total=%d generated=%d skipped=%d failed=%d cache=%s",
        a.db_id,
        total,
        generated,
        skipped,
        failed,
        out_path,
    )
    if failed:
        raise SystemExit(f"LLM description generation failed for {failed} column(s); rerun to retry")


if __name__ == "__main__":
    main()
