# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
from __future__ import annotations

import codecs
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from loguru import logger

_INTERPOLATION_PATTERN = re.compile(r"\$\{([^}:]*)(?::-([^}]*))?\}")
_DOUBLE_QUOTE_ESCAPE = re.compile(r'\\[\\\'"abfnrtv]')
_SINGLE_QUOTE_ESCAPE = re.compile(r"\\[\\']")


class BindingParseResult(NamedTuple):
    """Result of parsing one env file line."""

    skipped: bool
    success: bool
    key: str
    value: str | None


class KeyParseResult(NamedTuple):
    """Result of parsing an env key token."""

    success: bool
    text: str
    next_index: int


class QuotedParseResult(NamedTuple):
    """Result of parsing a quoted env value token."""

    success: bool
    text: str
    next_index: int


def load_env_file(path: Path | str, *, override: bool = False, encoding: str = "utf-8") -> bool:
    """Parse a ``.env`` file and set variables in ``os.environ``.

    Existing environment variables are kept when ``override`` is ``False``.
    Variable references ``${NAME}`` and ``${NAME:-default}`` are expanded using
    earlier entries in the same file, then the current process environment.

    Args:
        path: Path to the env file.
        override: When ``True``, values from the file replace existing variables.
        encoding: Text encoding used to read the file.

    Returns:
        ``True`` when the file contains at least one valid assignment, else ``False``.
    """
    file_path = Path(path)
    content = file_path.read_text(encoding=encoding)
    bindings = _parse_env_file(content)
    if not bindings:
        return False

    resolved = _resolve_bindings(bindings, override=override)
    if not resolved:
        return False

    for key, value in resolved.items():
        if key in os.environ and not override:
            continue
        if value is not None:
            os.environ[key] = value
    return True


def _parse_env_file(content: str) -> list[tuple[str, str | None]]:
    """Parse raw env file text into ordered ``(key, value)`` pairs.

    Bare keys without ``=`` yield ``value=None``. Blank lines and comments are skipped.
    """
    bindings: list[tuple[str, str | None]] = []
    for line in content.splitlines():
        parsed = _parse_binding_line(line)
        if parsed.skipped or not parsed.success:
            continue
        bindings.append((parsed.key, parsed.value))
    return bindings


def _parse_binding_line(line: str) -> BindingParseResult:
    """Parse one env file line into a structured parse result."""
    index = 0
    length = len(line)

    while index < length and line[index] in " \t":
        index += 1
    if index >= length:
        return BindingParseResult(skipped=True, success=True, key="", value=None)
    if line[index] == "#":
        return BindingParseResult(skipped=True, success=True, key="", value=None)

    if line.startswith("export", index):
        index += len("export")
        while index < length and line[index] in " \t":
            index += 1

    key_result = _parse_key(line, index)
    if not key_result.success:
        logger.warning("Could not parse env file line: {}", line)
        return BindingParseResult(skipped=False, success=False, key="", value=None)
    index = key_result.next_index
    key_name = key_result.text

    while index < length and line[index] in " \t":
        index += 1
    if index >= length or line[index] != "=":
        return BindingParseResult(skipped=False, success=True, key=key_name, value=None)

    index += 1
    while index < length and line[index] in " \t":
        index += 1

    value = _parse_value(line, index)
    if value is None:
        logger.warning("Could not parse env file line: {}", line)
        return BindingParseResult(skipped=False, success=False, key="", value=None)
    return BindingParseResult(skipped=False, success=True, key=key_name, value=value)


def _parse_key(line: str, start: int) -> KeyParseResult:
    """Parse an env key starting at ``start``."""
    length = len(line)
    if start >= length:
        return KeyParseResult(success=False, text="", next_index=start)

    if line[start] == "'":
        end = line.find("'", start + 1)
        if end == -1:
            return KeyParseResult(success=False, text="", next_index=start)
        key_text = line[(start + 1) : end]
        next_index = end + 1
        return KeyParseResult(success=True, text=key_text, next_index=next_index)

    match = re.match(r"([^=#\s]+)", line[start:])
    if match is None:
        return KeyParseResult(success=False, text="", next_index=start)
    key = match.group(1)
    next_index = start + match.end()
    return KeyParseResult(success=True, text=key, next_index=next_index)


def _parse_value(line: str, start: int) -> str | None:
    """Parse an env value starting at ``start``."""
    length = len(line)
    if start >= length:
        return ""

    char = line[start]
    if char == "'":
        parsed = _parse_quoted_value(line, start, "'")
        return parsed.text if parsed.success else None
    if char == '"':
        parsed = _parse_quoted_value(line, start, '"')
        return parsed.text if parsed.success else None

    raw = line[start:]
    return re.sub(r"\s+#.*", "", raw).rstrip()


def _parse_quoted_value(line: str, start: int, quote: str) -> QuotedParseResult:
    """Parse a single- or double-quoted value."""
    if line[start] != quote:
        return QuotedParseResult(success=False, text="", next_index=start)

    index = start + 1
    chunks: list[str] = []
    escape_pattern = _DOUBLE_QUOTE_ESCAPE if quote == '"' else _SINGLE_QUOTE_ESCAPE

    while index < len(line):
        char = line[index]
        if char == "\\":
            candidate = line[index : (index + 2)]
            if len(candidate) == 2 and escape_pattern.fullmatch(candidate):
                chunks.append(candidate)
                index += 2
                continue
        if char == quote:
            raw = "".join(chunks)
            decoded = _decode_escapes(raw, quote)
            next_index = index + 1
            return QuotedParseResult(success=True, text=decoded, next_index=next_index)
        chunks.append(char)
        index += 1
    return QuotedParseResult(success=False, text="", next_index=start)


def _decode_escapes(raw: str, quote: str) -> str:
    """Decode backslash escapes inside a quoted env value."""
    pattern = _DOUBLE_QUOTE_ESCAPE if quote == '"' else _SINGLE_QUOTE_ESCAPE

    def replace(match: re.Match[str]) -> str:
        return codecs.decode(match.group(0), "unicode-escape")

    return pattern.sub(replace, raw)


def _resolve_bindings(
    bindings: list[tuple[str, str | None]],
    *,
    override: bool,
) -> dict[str, str | None]:
    """Expand references and return resolved key/value pairs in file order."""
    resolved: dict[str, str | None] = {}
    for name, value in bindings:
        if value is None:
            resolved[name] = None
            continue
        lookup = _build_lookup(resolved, override=override)
        resolved[name] = _expand_value(value, lookup)
    return resolved


def _build_lookup(resolved: Mapping[str, str | None], *, override: bool) -> dict[str, str | None]:
    """Build the lookup map used while expanding ``${...}`` references."""
    lookup: dict[str, str | None] = {}
    if override:
        lookup.update(os.environ)
        lookup.update(resolved)
    else:
        lookup.update(resolved)
        lookup.update(os.environ)
    return lookup


def _expand_value(value: str, lookup: Mapping[str, str | None]) -> str:
    """Expand ``${NAME}`` and ``${NAME:-default}`` placeholders in ``value``."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        result = lookup.get(name, default) if default is not None else lookup.get(name, "")
        return "" if result is None else result

    return _INTERPOLATION_PATTERN.sub(replace, value)
