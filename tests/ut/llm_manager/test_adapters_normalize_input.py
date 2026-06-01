# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

# pylint: disable=protected-access
from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter, coerce_chat_input_to_messages


class _NonLangChainRaw:
    __module__ = "dataagent.llm_client_bridge"


class _LangChainRaw:
    __module__ = "langchain_community.chat_models.fake"


def test_coerce_chat_input_to_messages_wraps_str() -> None:
    assert coerce_chat_input_to_messages("hello") == [{"role": "user", "content": "hello"}]


def test_coerce_chat_input_to_messages_passthrough_list() -> None:
    messages = [{"role": "user", "content": "hi"}]
    assert coerce_chat_input_to_messages(messages) is messages


def test_normalize_input_for_langchain_non_langchain_raw_accepts_str() -> None:
    adapter = LangChainChatModelAdapter(_NonLangChainRaw(), config=SimpleNamespace())
    normalized = adapter._normalize_input_for_langchain("hello")
    assert normalized == [{"role": "user", "content": "hello"}]


def test_normalize_input_for_langchain_langchain_raw_accepts_str() -> None:
    adapter = LangChainChatModelAdapter(_LangChainRaw(), config=SimpleNamespace())
    normalized = adapter._normalize_input_for_langchain("hello")
    assert len(normalized) == 1
    assert isinstance(normalized[0], HumanMessage)
    assert normalized[0].content == "hello"
