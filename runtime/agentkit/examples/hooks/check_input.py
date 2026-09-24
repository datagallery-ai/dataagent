"""Inspect native state without requiring a framework decorator or import."""


def handle(state, runtime, *, params):
    limit = params.get("max_chars", 4000)
    if type(limit) is not int or limit <= 0:
        raise ValueError("max_chars must be a positive integer")
    # Check the newest user message, not previously accepted conversation history.
    message = next((item for item in reversed(state.get("messages", []))
                    if item.type == "human"), None)
    if message is not None and isinstance(message.content, str) and len(message.content) > limit:
        raise ValueError(f"Input exceeds {limit} characters")
