from __future__ import annotations

from typing import Any, Dict

try:
    from ... import config
except ImportError:
    import config

from ...common.atomic_io import atomic_write_json, read_json
from ..core.llm_utils import safe_json_chat
from ..models.step1_models import StepResult
from ..sources.bird_metadata import load_table_description_map
from .base import StepBuilder

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


class Step1bColumnDescBuilder(StepBuilder):
    step_name = "step1b_enhance_column_desc"

    def _run_impl(self) -> StepResult:
        columns_info_path = self.context.artifacts.rest_cache_dir / "table_columns_info_cache.json"
        sample_values_path = self.context.artifacts.rest_cache_dir / "columns_sample_values_cache.json"
        columns_info_cache: Dict[str, Dict[str, list[Dict[str, Any]]]] = read_json(columns_info_path, default={})
        sample_values_cache = read_json(sample_values_path, default={})

        processed_count = 0
        skipped_count = 0
        flush_pending = 0
        flush_interval = max(1, int(config.STEP1B_FLUSH_INTERVAL))

        llm_client = self.context.get_llm_client()
        completed = set(self.progress.completed_keys()) if not self.context.force_rerun else set()

        def _flush_cache() -> None:
            atomic_write_json(columns_info_path, columns_info_cache)

        for db_id, table_map in columns_info_cache.items():
            if not self.context.should_process_db(db_id):
                continue
            for table_name, columns in table_map.items():
                desc_map = load_table_description_map(self.context.bird_db_dir, db_id, table_name)
                for column in columns or []:
                    column_id = str(column.get("column_id") or "").strip()
                    if not column_id:
                        continue
                    column_name = str(column.get("column_name") or "").strip()
                    staging_path = self.context.artifacts.step1b_column_staging_path(db_id, table_name, column_name)

                    # 续跑 1：state 已记录 → 跳过
                    if column_id in completed:
                        skipped_count += 1
                        continue

                    # 续跑 2：staging 已存在但未登记 → 回放 + 补登记
                    if not self.context.force_rerun and staging_path.exists():
                        try:
                            staged_payload = read_json(staging_path, default={})
                            staged_desc = str(staged_payload.get("desc_short") or "").strip()
                        except Exception:
                            staged_desc = ""
                        if staged_desc:
                            column["desc_short"] = staged_desc
                            column["column_description_short"] = staged_desc
                            self.progress.add_completed_key(column_id)
                            completed.add(column_id)
                            skipped_count += 1
                            continue

                    desc_short = str(column.get("desc_short") or column.get("column_description_short") or "").strip()
                    desc_row = desc_map.get(column_name) or desc_map.get(column_name.lower()) or {}
                    csv_column_name = str(desc_row.get("column_name") or "").strip()
                    csv_column_description = str(desc_row.get("column_description") or "").strip()
                    # 走到这里说明缓存缺失（state 未登记且 staging 不存在），
                    # 必须通过 LLM 重新生成短描述；csv 字段仅作为 prompt 输入与失败时的 fallback。
                    should_generate = True
                    if should_generate:
                        prompt = _COLUMN_DESC_PROMPT.format(
                            original_column_name=column_name or 'UNKNOWN',
                            csv_column_name=csv_column_name or 'EMPTY',
                            csv_column_description=csv_column_description or 'EMPTY',
                        )
                        parsed = safe_json_chat(
                            llm_client,
                            prompt,
                            default={},
                            temperature=float(config.STEP1B_LLM_TEMPERATURE),
                        )
                        desc_short = str(parsed.get("simple_description") or parsed.get("column_description_short") or "").strip()
                    if not desc_short:
                        desc_short = str(csv_column_name or csv_column_description or column.get("desc_simple") or column_name).strip()
                    column["desc_short"] = desc_short
                    column["column_description_short"] = desc_short

                    # 列级 staging：atomic 写入（输出在前）
                    atomic_write_json(
                        staging_path,
                        {
                            "column_id": column_id,
                            "db_id": db_id,
                            "table_name": table_name,
                            "column_name": column_name,
                            "desc_short": desc_short,
                        },
                    )

                    processed_count += 1
                    flush_pending += 1
                    # 达到 FLUSH_INTERVAL：flush 整 cache → 再 add_completed_key
                    if flush_pending >= flush_interval:
                        _flush_cache()
                        flush_pending = 0
                    self.progress.add_completed_key(column_id)
                    completed.add(column_id)

        # 收尾：总是 flush 一次以保证最终一致
        _flush_cache()
        return StepResult(
            step_name=self.step_name,
            status="success",
            processed_count=processed_count,
            skipped_count=skipped_count,
            outputs=[str(columns_info_path)] if processed_count > 0 else [],
        )
