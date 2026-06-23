# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""OpenJiuWen HITL adapters for DataAgent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def build_hitl_rail(enabled: bool) -> Any | None:
    """Build the Jiuwen ask-user rail when YAML enables human feedback."""
    if not enabled:
        return None

    from openjiuwen.harness.rails import AskUserRail

    return AskUserRail()


def build_interactive_input(
    human_feedback: Any,
    *,
    interrupt_id: str | None = None,
) -> Any:
    """Translate DataAgent's public feedback payload to Jiuwen InteractiveInput."""
    from openjiuwen.core.session import InteractiveInput

    if isinstance(human_feedback, InteractiveInput):
        return human_feedback

    interactive_input = InteractiveInput()
    if isinstance(human_feedback, Mapping):
        user_inputs = human_feedback.get("user_inputs")
        if isinstance(user_inputs, Mapping):
            _add_user_inputs(interactive_input, user_inputs)
            return interactive_input

        responses = human_feedback.get("responses")
        if responses is not None:
            if isinstance(responses, (str, bytes)) or not isinstance(responses, Sequence):
                raise ValueError("human_feedback.responses must be a list of response mappings")
            for index, response in enumerate(responses):
                if not isinstance(response, Mapping):
                    raise ValueError(f"human_feedback.responses[{index}] must be a mapping")
                response_id, response_value = _normalize_response(response, None)
                interactive_input.update(response_id, response_value)
            if not interactive_input.user_inputs:
                raise ValueError("human_feedback.responses must not be empty")
            return interactive_input

    response_id, response_value = _normalize_response(human_feedback, interrupt_id)
    interactive_input.update(response_id, response_value)
    return interactive_input


def _add_user_inputs(interactive_input: Any, user_inputs: Mapping[Any, Any]) -> None:
    if not user_inputs:
        raise ValueError("human_feedback.user_inputs must not be empty")
    for raw_id, value in user_inputs.items():
        response_id = _normalize_interrupt_id(raw_id)
        interactive_input.update(response_id, value)


def _normalize_response(response: Any, explicit_id: str | None) -> tuple[str, Any]:
    response_id = _normalize_interrupt_id(explicit_id, required=False)

    if isinstance(response, Mapping):
        embedded_id = (
            response.get("interrupt_id")
            or response.get("tool_call_id")
            or response.get("id")
        )
        if embedded_id is not None:
            normalized_embedded_id = _normalize_interrupt_id(embedded_id)
            if response_id and response_id != normalized_embedded_id:
                raise ValueError("interrupt_id conflicts with human_feedback interrupt identifier")
            response_id = normalized_embedded_id

        if "payload" in response:
            value = response["payload"]
        elif "answers" in response:
            value = {"answers": response["answers"]}
        elif "answer" in response:
            value = {"answer": response["answer"]}
        elif "value" in response:
            value = response["value"]
        else:
            control_keys = {"interrupt_id", "tool_call_id", "id"}
            value = {key: item for key, item in response.items() if key not in control_keys}
    else:
        value = response

    if not response_id:
        raise ValueError(
            "interrupt_id is required when resuming HITL; use the ID returned by the interrupt response"
        )
    if value is None:
        raise ValueError("human_feedback value must not be null")
    return response_id, value


def _normalize_interrupt_id(value: Any, *, required: bool = True) -> str:
    normalized = str(value).strip() if value is not None else ""
    if required and not normalized:
        raise ValueError("interrupt_id must not be empty")
    return normalized
