"""Bird-only path validation and request-local diagnostic state."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def validate_bird_path_inputs(config: Any, state: Mapping[str, Any] | None = None, *, session_id: Any = None) -> None:
    """Reject path components before Bird SDK logging or workspace creation."""
    values = {"session_id argument": session_id}
    values.update({name: config.get(name) for name in ("SESSION_ID", "RUN_ID")})
    if isinstance(state, Mapping):
        values.update(
            {name: state.get(name) for name in ("session_id", "run_id", "_parent_session_id", "_parent_run_id")}
        )
    for name, value in values.items():
        if value is None or value == "":
            continue
        component = str(value)
        if component == "." or any(part in component for part in ("..", "/", "\\", "\x00")):
            raise ValueError(f"Bird {name} must be a single safe path component.")


@dataclass
class BirdContextDump:
    directory: Path
    sequence: int = 0
    active: bool = True


_CURRENT_DUMP: ContextVar[BirdContextDump | None] = ContextVar("bird_context_dump", default=None)


def current_context_dump() -> BirdContextDump | None:
    return _CURRENT_DUMP.get()


@contextmanager
def context_dump_scope(dump: BirdContextDump | None) -> Iterator[None]:
    token = _CURRENT_DUMP.set(dump)
    try:
        yield
    finally:
        if dump is not None:
            dump.active = False
        _CURRENT_DUMP.reset(token)


async def stream_without_context_dump(stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
    """Suppress inherited chat dumps without holding a token across yields."""
    try:
        while True:
            with context_dump_scope(None):
                try:
                    item = await anext(stream)
                except StopAsyncIteration:
                    return
            yield item
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            with context_dump_scope(None):
                await close()
