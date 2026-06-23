# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""OpenJiuWen context processor adapter."""

from __future__ import annotations

from typing import Any

from dataagent.core.deep_agent.spec import ContextCompressionSpec


def build_context_processor_rail(spec: ContextCompressionSpec) -> Any:
    """Build Jiuwen's preset processors with DataAgent threshold overrides."""
    from openjiuwen.harness.rails.context_engineer import ContextProcessorRail

    return ContextProcessorRail(
        preset=True,
        processors=[
            (
                "DialogueCompressor",
                {
                    "tokens_threshold": spec.compress_token_limit,
                    "messages_threshold": spec.compress_message_cnt,
                },
            )
        ],
    )
