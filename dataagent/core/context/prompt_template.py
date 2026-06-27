# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Prompt templates used by the context subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Template

CONTEXT_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class ContextPromptTemplate:
    """A small file-backed Jinja prompt template for context prompts."""

    content: str

    @classmethod
    def from_context_prompt(cls, name: str) -> "ContextPromptTemplate":
        """Load a context prompt by stem name from ``core/context/prompts``."""
        prompt_path = CONTEXT_PROMPT_DIR / f"{name}.md"
        if not prompt_path.is_file():
            raise ValueError(f"Context prompt not found: {prompt_path}")
        return cls(prompt_path.read_text(encoding="utf-8"))

    def apply_prompt_template(self, **variables: Any) -> str:
        """Render the prompt content with Jinja variables."""
        return Template(self.content).render(**variables)
