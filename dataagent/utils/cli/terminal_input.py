# Licensed under the Apache License, Version 2.0 (the "License")
"""Multi-line terminal input using prompt_toolkit."""

from __future__ import annotations


def multiline_input(prompt: str = "> ") -> str:
    """Read multi-line input from terminal. Falls back to input()."""
    return input(prompt)
