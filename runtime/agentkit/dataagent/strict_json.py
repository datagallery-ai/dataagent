"""Strict JSON object reader shared by configuration layers and extension declarations."""

import json
import math
from pathlib import Path


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            # Keys, like values, can contain credentials; do not echo either.
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite numbers are not valid JSON")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is outside the supported finite range")
    return number


def read_json(path: Path) -> dict:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_object,
            parse_constant=_constant, parse_float=_float,
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Parser exceptions may quote source content containing credentials.
        raise ValueError(f"Invalid JSON in {path}") from None
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from None
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value
