"""Inspect the native ToolCallRequest before tool execution."""


def handle(request, *, params):
    if request.tool_call["name"] in params.get("blocked_tools", []):
        raise ValueError("Tool is blocked by the configured policy")
