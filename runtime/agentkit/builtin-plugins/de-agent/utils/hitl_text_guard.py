"""Question-ending detector retained from !453/!571."""

import re

HUMAN_FEEDBACK_TOOL_NAME = "request_human_feedback"

_GUARD_FIRE_COUNT_KEY = "text_question_guard.fire_count"

_MAX_GUARD_FIRES = 2

_MAX_PENDING_ACTION_CHARS = 2000

_AWAITING_USER_RE = re.compile(
    r"(请问|请确认|请您|您确认|您倾向|您选择|您决定|是否同意|是否确认|是否需要|"
    r"确认一下|帮我确认|您能|告诉我|您指定|吗\s*[？?]|呢\s*[？?])"
)


def _ends_awaiting_user(content: str) -> bool:
    """判定回复是否以"等待用户答复"收尾。

    规则：
    最后一个非空行需含强提问标记，且以问号/吗/呢收尾或含 请问/请确认/请您——
    交付说明（"…已修正为 X。"）不命中。
    """
    if not content:
        return False
    lines = [line for line in content.strip().splitlines() if line.strip()]
    if not lines:
        return False
    last = lines[-1].rstrip()
    if not _AWAITING_USER_RE.search(last):
        return False
    ends_question = last.endswith(("？", "?", "吗", "呢"))
    has_explicit_ask = any(kw in last for kw in ("请问", "请确认", "请您"))
    return ends_question or has_explicit_ask
