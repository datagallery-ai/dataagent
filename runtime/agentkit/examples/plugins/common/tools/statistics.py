"""Deterministic, side-effect-free numeric tool for the minimum product case."""

import math

from langchain_core.tools import ToolException, tool


@tool
def summarize_numbers(numbers: list[float]) -> dict[str, float | int]:
    """Compute count, sum, mean, minimum and maximum of a nonempty finite numeric list."""
    if len(numbers) > 10_000:
        raise ValueError("At most 10000 numbers are accepted per statistics call")
    if not numbers or any(not math.isfinite(number) for number in numbers):
        raise ToolException("Provide a nonempty list of finite numbers")
    try:
        total = math.fsum(numbers)
    except OverflowError as error:
        raise ToolException("The sum is outside the supported numeric range") from error
    if not math.isfinite(total):
        raise ToolException("The sum is outside the supported numeric range")
    return {
        "count": len(numbers), "sum": total, "mean": total / len(numbers),
        "min": min(numbers), "max": max(numbers),
    }
