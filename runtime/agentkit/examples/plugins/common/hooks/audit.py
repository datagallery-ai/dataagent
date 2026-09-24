"""Six ordinary Python Hooks; lifecycle observations, not terminal/error callbacks.

Record event and Agent labels plus tool identity, never message text, arguments or results.
"""

import logging

logger = logging.getLogger("dataagent.audit")


def _record(event, params):
    logger.info("hook.%s agent=%s", event, params.get("agent", "dataagent-v2"))


def before_agent(state, runtime, *, params):
    _record("before_agent", params)


def after_agent(state, runtime, *, params):
    _record("after_agent", params)


def before_model(state, runtime, *, params):
    _record("before_model", params)


def after_model(state, runtime, *, params):
    _record("after_model", params)


def before_tool(request, *, params):
    logger.info("hook.before_tool agent=%s tool=%s call=%s",
                params.get("agent", "dataagent-v2"), request.tool_call["name"], request.tool_call["id"])


def after_tool(request, result, *, params):
    logger.info("hook.after_tool agent=%s tool=%s call=%s",
                params.get("agent", "dataagent-v2"), request.tool_call["name"], request.tool_call["id"])
