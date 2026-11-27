# 工具

工具是插件清单里的一个 Python 函数，或已经构造好的 LangChain `BaseTool`。参数说明来自函数签名和 docstring，或来自 `BaseTool` 的 schema。产品配置里没有第二套参数表。

## 最小实现

`tools/statistics.py`：

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

`.plugin.yaml`：

```yaml
schema_version: 3
id: common
tools:
  summarize_numbers:
    entrypoint: tools/statistics.py:summarize_numbers
```

模型用 `common__summarize_numbers` 调用此工具。普通函数由宿主包装成工具；导出 `BaseTool` 时，宿主复用其参数 schema 并以全名注册。字段和作用域见[插件清单](../../../reference/extension-schema.md#plugin-manifest)，完整数值校验示例见 [statistics.py](../../../../../examples/plugins/common/tools/statistics.py)。

<a id="tool-runtime-clients"></a>

## 访问模型和语义客户端

需要另一个已配置模型时，增加 `ToolRuntime` 参数。不要在工具里读 `.env`。

```python
from langchain.tools import ToolRuntime
from dataagent import AgentContext

async def summarize(text: str, runtime: ToolRuntime[AgentContext]) -> str:
    """Summarize text with the configured fast model."""
    model = runtime.context.get_model("fast")
    return (await model.ainvoke(text, config=runtime.config)).text
```

`get_model("fast")` 读取 `models.fast`。未知别名失败。客户端在当前 Agent 内复用。

语义服务配置存在时，`runtime.context.semantic_client` 是宿主或 SDK 拥有的 `SemanticServiceClient`。未配置时它是 `None`。用法见 [语义服务](semantic-service.md)。完整示例见 [llm-utils](../../../../../examples/plugins/llm-utils/README.md)。

把 `runtime.config` 传给模型以沿用调用上下文。普通工具直接发起的模型调用与 Agent 循环分开，不作为独立模型 span 导出。详见[执行 Trace](../../../../development/architecture/execution-traces.md#trace-data-boundaries)。

## 运行限制

主 Agent 和标准子代理各自执行[工具调用限额](../../../reference/configuration.md#dataagent)，新的一轮重新计数。

`ToolException` 会变成一条可纠正的工具消息：`Invalid tool input...`。其他异常按原生工具错误处理。

工具和 shell 能访问宿主进程允许的路径。会话目录只组织文件，不隔离访问。见 [安全边界](../../../../development/architecture/security-boundaries.md)。
