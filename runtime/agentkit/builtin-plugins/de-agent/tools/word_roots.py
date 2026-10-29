"""Expose the original batch validator for manual root checks and candidate lookup."""

from langchain.tools import ToolRuntime
from utils.word_root_validator import WordRootContext
from utils.word_root_validator import validate_word_roots as _validate_word_roots

from dataagent.models import ModelContext


async def validate_word_roots(
    items: list[dict[str, str | None]],
    runtime: ToolRuntime[ModelContext],
    k: int = 5,
    llm_batch_size: int = 5,
) -> dict:
    """Validate up to 20 unique roots and return exact/regex matches or candidates.

    Each item requires english_name and optionally chinese_description. Split
    full table/column names on underscores before calling; omit database names.
    k limits candidates per root (1-20); llm_batch_size limits unresolved roots
    per model request (1-20). Missing CSV returns validation_status=skipped,
    which is not evidence of a match. DDL file hooks run checks automatically.
    """
    try:
        return await _validate_word_roots(
            items, k, llm_batch_size, _tool_context=WordRootContext(runtime.context)
        )
    except Exception as exc:
        return {"validation_status": "error", "error_msg": str(exc), "results": []}
