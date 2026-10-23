"""Native tool error handling for recoverable semantic-service failures."""

from functools import wraps

import httpx
from langchain_core.tools import ToolException, tool


def semantic_tool(function):
    """Expose a synchronous semantic function with recoverable, credential-free errors."""

    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (httpx.HTTPError, ValueError) as exc:
            raise ToolException(
                f"{function.__name__} failed ({type(exc).__name__}). "
                "This is not evidence that a table or entity is absent. "
                "Check arguments or use a different metadata endpoint."
            ) from exc

    native = tool(invoke)
    native.handle_tool_error = True
    return native
