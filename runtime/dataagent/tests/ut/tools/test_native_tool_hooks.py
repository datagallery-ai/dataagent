"""Focused tests for native Deep Agents tool-hook configuration."""

from dataagent.core.deepagents.config.tool_hooks import ToolHookConfigCompiler

_PRE = "dataagent.actions.tools.hooks.examples.example_hooks.audit_pre"
_POST = "dataagent.actions.tools.hooks.examples.example_hooks.audit_post"


def test_tool_hook_compiler_supports_local_mcp_and_a2a_sources() -> None:
    """Compatible YAML hook locations compile into one native middleware map."""
    hook_config = {"pre": [_PRE], "post": [_POST]}
    config = {
        "TOOLS": {
            "local_functions": [{"name": "local_probe", "module": "example", "hooks": hook_config}],
            "mcp_servers": [{"server_id": "warehouse", "hooks": hook_config}],
            "A2A": [{"catalog": {"base_url": "http://127.0.0.1", "hooks": hook_config}}],
        }
    }

    middleware = ToolHookConfigCompiler(config).compile()

    assert middleware is not None
    assert set(middleware._hooks) == {("local", "local_probe"), ("mcp", "warehouse"), ("a2a", "catalog")}
