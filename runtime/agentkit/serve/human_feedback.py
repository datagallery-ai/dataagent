"""Bind business-clarification replies to interrupts owned by the current checkpoint."""

from ag_ui.core import ResumeEntry
from langchain_core.messages import AIMessage


def pending_feedback(snapshot):
    pending = []
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = item.value
            if isinstance(value, dict) and value.get("type") == "human_feedback":
                pending.append({"interruptId": item.id, **value})
    return pending


def resume_input(body, snapshot):
    pending = pending_feedback(snapshot)
    entries = list(body.resume or [])
    if not entries and pending:
        if len(pending) != 1:
            raise ValueError("Multiple questions are pending; supply an explicit interruptId")
        entries = [
            ResumeEntry(
                interrupt_id=pending[0]["interruptId"],
                status="resolved",
                payload=body.messages[0].content,
            )
        ]
    if entries:
        expected = {item["interruptId"] for item in pending}
        actual = [entry.interrupt_id for entry in entries]
        if not expected or len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("Feedback interrupt is stale or does not belong to this session")
        for entry in entries:
            if entry.status == "resolved" and (
                not isinstance(entry.payload, str) or not entry.payload.strip()
            ):
                raise ValueError("Human feedback must be nonempty text")
        return body.model_copy(update={"resume": entries, "messages": []})
    return body


def question_messages(snapshot):
    return [
        AIMessage(content=item["message"], id="hitl_question_" + item["interruptId"])
        for item in pending_feedback(snapshot)
    ]
