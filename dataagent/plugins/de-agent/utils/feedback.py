"""Session-scoped evidence of explicitly rejected source tables."""

import json
import re

from utils.data_task_ir.session_store import atomic_json


def load_rejections(context):
    path = context.session.outputs / ".runtime" / "rejected-tables.json"
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record_feedback(context, answer):
    if not re.search(r"拒绝|不要|不使用|不是|不符合|不对", str(answer)):
        return
    tables = re.findall(r"\b[a-zA-Z][\w]*\.[a-zA-Z][\w]*\b", str(answer))
    if not tables:
        return
    ledger = load_rejections(context)
    ledger["rejected_tables"] = sorted(
        set(ledger.get("rejected_tables", [])) | {t.lower() for t in tables}
    )
    ledger["feedback"] = str(answer)
    ledger["rounds"] = ledger.get("rounds", 0) + 1
    atomic_json(context.session.outputs / ".runtime" / "rejected-tables.json", ledger)


def filter_candidates(context, tables):
    rejected = set(load_rejections(context).get("rejected_tables", []))
    return [t for t in tables if str(t.get("table", "")).lower() not in rejected]
