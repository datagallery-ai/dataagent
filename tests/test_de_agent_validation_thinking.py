# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
# ============================================================================
# Unit tests for the validation chain's thinking-mode switch.
#
# The validation chain (DataOps SQL validation + failure-log analysis, deliverable
# quality gate, SQL rule repair, metadata grounding) runs short, structured-output
# prompts. Thinking mode is pure overhead there — and worse, it can mix ``<think>``
# fragments into the JSON body. So these calls must not enable it by default.
#
# The switch is **per tool configuration** (``config.enable_thinking``), NOT a
# global environment variable: changing one tool's config must not affect the
# others, nor the main agent's planner.
#
# These tests lock two invariants:
#
# 1. ``validation_llm_kwargs`` returns ``{"enable_thinking": False}`` for slots
#    that recognise the flag (bailian endpoint, or a slot that had it turned on),
#    and ``{}`` for slots that do not — sending an unknown field to a strict
#    gateway can get the request rejected.
# 2. The per-call kwarg **overrides** the slot's own ``enable_thinking: true``,
#    without mutating the slot.
# ============================================================================

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from langchain_openai import ChatOpenAI

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"
LOADER = PythonLoader()
helpers = sys.modules[
    LOADER.load(PLUGIN, "utils/validation_model.py:validation_llm_kwargs").__module__
]
THINKING_FIELD = helpers.THINKING_FIELD
supports_thinking_toggle = helpers.supports_thinking_toggle
thinking_enabled_from_config = helpers.thinking_enabled_from_config
validation_llm_kwargs = helpers.validation_llm_kwargs


def _client(
    *,
    model: str = "DeepSeek_V4_Flash_Client",
    api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    extra_body: dict | None = None,
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        base_url=api_base,
        api_key="sk-test",
        extra_body=extra_body or {},
    )


class TestValidationThinkingDisabled:
    """The default must be thinking-off for every slot that understands the flag."""

    def test_bailian_endpoint_disables_thinking(self):
        llm = _client()
        assert validation_llm_kwargs(llm) == {"extra_body": {THINKING_FIELD: False}}

    def test_slot_with_thinking_on_gets_overridden_to_off(self):
        llm = _client(extra_body={THINKING_FIELD: True})
        assert validation_llm_kwargs(llm) == {"extra_body": {THINKING_FIELD: False}}

    def test_unknown_gateway_sends_no_flag(self):
        """A strict OpenAI-compatible endpoint must not receive an unknown field."""
        llm = _client(api_base="https://api.openai.com/v1")
        assert validation_llm_kwargs(llm) == {}

    def test_explicit_enable_turns_thinking_on(self):
        llm = _client()
        assert validation_llm_kwargs(llm, enable_thinking=True) == {
            "extra_body": {THINKING_FIELD: True}
        }

    def test_explicit_enable_does_not_touch_unknown_gateway(self):
        """Even when asked to enable, an unrecognised gateway gets no new field."""
        llm = _client(api_base="https://api.openai.com/v1")
        assert validation_llm_kwargs(llm, enable_thinking=True) == {}

    def test_no_llm_yields_no_kwargs(self):
        assert validation_llm_kwargs(None) == {}


class TestThinkingEnabledFromConfig:
    """The switch comes from the tool config, defaulting to OFF."""

    def test_missing_config_defaults_to_disabled(self):
        assert thinking_enabled_from_config(None) is False
        assert thinking_enabled_from_config({}) is False

    def test_non_dict_config_defaults_to_disabled(self):
        assert thinking_enabled_from_config("nope") is False
        assert thinking_enabled_from_config([1, 2]) is False

    def test_explicit_false(self):
        assert thinking_enabled_from_config({THINKING_FIELD: False}) is False

    def test_explicit_true(self):
        assert thinking_enabled_from_config({THINKING_FIELD: True}) is True

    def test_string_truthy_values(self):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            assert thinking_enabled_from_config({THINKING_FIELD: raw}) is True

    def test_string_falsy_values(self):
        for raw in ("0", "false", "no", "off", ""):
            assert thinking_enabled_from_config({THINKING_FIELD: raw}) is False

    def test_non_bool_non_str_is_disabled(self):
        assert thinking_enabled_from_config({THINKING_FIELD: 1}) is False
        assert thinking_enabled_from_config({THINKING_FIELD: None}) is False


class TestSupportsThinkingToggle:
    def test_bailian_endpoint_is_supported(self):
        assert supports_thinking_toggle(_client()) is True

    def test_slot_with_thinking_on_is_supported(self):
        llm = _client(api_base="https://x/v1", extra_body={THINKING_FIELD: True})
        assert supports_thinking_toggle(llm) is True

    def test_plain_gateway_is_not_supported(self):
        assert supports_thinking_toggle(_client(api_base="https://api.openai.com/v1")) is False

    def test_none_is_not_supported(self):
        assert supports_thinking_toggle(None) is False


class TestPerCallOverrideReachesPayload:
    """The kwarg must win over the slot's own setting in the outbound request body."""

    def test_per_call_flag_overrides_slot_flag(self):
        llm = _client(extra_body={THINKING_FIELD: True, "temperature": 0.3})
        payload = llm._get_request_payload(
            [{"role": "user", "content": "hi"}], **validation_llm_kwargs(llm)
        )
        assert payload["extra_body"][THINKING_FIELD] is False
        # Other slot options survive untouched.
        assert payload["extra_body"]["temperature"] == 0.3

    def test_slot_is_not_mutated_by_per_call_override(self):
        llm = _client(extra_body={THINKING_FIELD: True})
        llm._get_request_payload([{"role": "user", "content": "hi"}], **validation_llm_kwargs(llm))
        assert llm.extra_body[THINKING_FIELD] is True

    def test_adapter_raw_client_is_resolved(self):
        """The helper must look through the adapter to the underlying client."""
        adapter = SimpleNamespace(
            raw=_client(api_base="https://dashscope.aliyuncs.com/compatible-mode/v1")
        )
        assert validation_llm_kwargs(adapter) == {"extra_body": {THINKING_FIELD: False}}
