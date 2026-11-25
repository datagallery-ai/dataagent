"""Load the same optional SQL guidance for Local generation and review."""

from pathlib import Path

from langchain_core.messages import SystemMessage

from nl2sql_local.tools.database import configuration

_RULES_DIR = Path(__file__).resolve().parents[2] / "prompts"


def sql_rules_prompt(config):
    name = config.user_sql_rules
    if name is None:
        return ""
    rules = (_RULES_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
    return (
        f"Additional SQL guidance ({name}). Apply it where consistent with the original "
        "question, supplied evidence, observed database facts, and the Local instructions "
        "on preserving requested fields, row grain and aggregation scope. Those take "
        "precedence if the guidance conflicts.\n\n"
        f"{rules}"
    )


def prepare_sql_rules(state, runtime, *, params):
    config, _ = configuration(runtime.context)
    prompt = sql_rules_prompt(config)
    if prompt:
        return {"messages": [SystemMessage(content=prompt)]}
    return None
