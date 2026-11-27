# Tools

Declare a Python function or an existing LangChain `BaseTool` in the plugin manifest. Parameters come from the function signature and docstring or from the tool schema.

## Minimal implementation

`tools/statistics.py`:

```python
from langchain_core.tools import ToolException

def summarize_numbers(numbers: list[float]) -> dict[str, float | int]:
    """Return count, sum, mean, minimum and maximum for a list of numbers."""
    if not numbers:
        raise ToolException("Provide a nonempty list of numbers")
    total = sum(numbers)
    return {
        "count": len(numbers),
        "sum": total,
        "mean": total / len(numbers),
        "min": min(numbers),
        "max": max(numbers),
    }
```

`.plugin.yaml`:

```yaml
schema_version: 3
id: common
tools:
  summarize_numbers:
    entrypoint: tools/statistics.py:summarize_numbers
```

The model calls `common__summarize_numbers`. The host wraps ordinary functions. For a `BaseTool`, it reuses the schema and registers the tool under the fully qualified name. See [plugin manifest](../../../reference/extension-schema.md#plugin-manifest) and the [complete validation example](../../../../../examples/plugins/common/tools/statistics.py).

<a id="tool-runtime-clients"></a>

## Access models and the semantic client

Use `ToolRuntime` for other configured models. Read model configuration through the runtime rather than loading `.env` in the tool.

```python
from langchain.tools import ToolRuntime
from dataagent import AgentContext

async def summarize(text: str, runtime: ToolRuntime[AgentContext]) -> str:
    """Summarize text with the configured fast model."""
    model = runtime.context.get_model("fast")
    return (await model.ainvoke(text, config=runtime.config)).text
```

`get_model("fast")` selects `models.fast`. An invalid alias fails. Clients are reused within the current agent.

When configured, `runtime.context.semantic_client` is the host- or SDK-owned `SemanticServiceClient`. Otherwise it is `None`. See [semantic service](semantic-service.md) and the [llm-utils example](../../../../../examples/plugins/llm-utils/README.md).

Pass `runtime.config` to carry the call context. Direct model calls inside ordinary tools run separately from the Agent loop and are not exported as independent model spans. See [trace data boundaries](../../../../development/architecture/execution-traces.md#trace-data-boundaries).

## Execution limits

Root and standard subagents each enforce their [tool-call limits](../../../reference/configuration.md#dataagent). Counts reset for a new turn.

A `ToolException` becomes a correctable `Invalid tool input...` tool message. Other exceptions follow native tool error handling. File and shell tools access paths allowed by the host process. The session directory organizes files. See [security boundaries](../../../../development/architecture/security-boundaries.md) for access rules.
