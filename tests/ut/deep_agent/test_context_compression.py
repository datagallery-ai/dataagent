# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Context compression YAML adapter tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dataagent.core.deep_agent.adapter import DeepAgentAdapter
from dataagent.core.deep_agent.spec import ContextCompressionSpec, DeepAgentBuildSpec
from dataagent.utils.constants import (
    DEFAULT_COMPRESS_MESSAGE_CNT,
    DEFAULT_COMPRESS_TOKEN_LIMIT,
)


def test_context_compression_uses_legacy_defaults() -> None:
    spec = DeepAgentBuildSpec.from_config({}).context_compression

    assert spec == ContextCompressionSpec(
        compress_token_limit=DEFAULT_COMPRESS_TOKEN_LIMIT,
        compress_message_cnt=DEFAULT_COMPRESS_MESSAGE_CNT,
    )


def test_context_compression_reads_legacy_yaml_thresholds() -> None:
    spec = DeepAgentBuildSpec.from_config(
        {
            "CONTEXT": {
                "compress_token_limit": 48000,
                "compress_message_cnt": 120,
                "file_node_threshold": 500,
            }
        }
    ).context_compression

    assert spec == ContextCompressionSpec(
        compress_token_limit=48000,
        compress_message_cnt=120,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("compress_token_limit", 0),
        ("compress_token_limit", True),
        ("compress_message_cnt", -1),
        ("compress_message_cnt", "200"),
    ],
)
def test_context_compression_rejects_invalid_thresholds(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=rf"CONTEXT\.{field} must be a positive integer"):
        DeepAgentBuildSpec.from_config({"CONTEXT": {field: value}})


def test_context_processor_rail_uses_preset_and_overrides_dialogue_thresholds() -> None:
    rail = DeepAgentAdapter(
        {
            "CONTEXT": {
                "compress_token_limit": 48000,
                "compress_message_cnt": 120,
            }
        }
    ).build_context_processor_rail()
    react_config = SimpleNamespace(
        model_config_obj=None,
        model_client_config=None,
        context_processors=None,
    )
    agent = SimpleNamespace(
        react_agent=SimpleNamespace(_config=react_config),
        system_prompt_builder=None,
    )

    rail.init(agent)

    processors = dict(react_config.context_processors)
    assert list(processors) == [
        "MessageSummaryOffloader",
        "DialogueCompressor",
        "CurrentRoundCompressor",
        "RoundLevelCompressor",
    ]
    dialogue = processors["DialogueCompressor"]
    assert dialogue.tokens_threshold == 48000
    assert dialogue.messages_threshold == 120
    assert dialogue.messages_to_keep == 10
    assert dialogue.compression_target_tokens == 1800
