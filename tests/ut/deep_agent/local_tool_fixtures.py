"""Functions imported dynamically by local tool builder tests."""

from __future__ import annotations

import asyncio
import threading
from typing import Any


def add_numbers(a: int, b: int = 1) -> int:
    """Add two numbers.

    Args:
        a: First number.
        b: Second number.
    """
    return a + b


def current_thread_id() -> int:
    """Return the worker thread identifier."""
    return threading.get_ident()


async def async_echo(message: str) -> str:
    """Return a message asynchronously."""
    await asyncio.sleep(0)
    return message


def needs_tool_context(value: str, *, _tool_context: Any) -> str:
    """A legacy context-dependent tool."""
    return value


def generated_values() -> Any:
    """Yield generated values."""
    yield 1


NOT_CALLABLE = "not callable"
