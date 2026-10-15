# 工具

工具是插件清单里的一个 Python 函数，或已经构造好的 LangChain `BaseTool`。参数说明来自函数签名和 docstring，或来自 `BaseTool` 的 schema。产品配置里没有第二套参数表。

## 最小实现

`tools/statistics.py`：

```python
def summarize_numbers(values: list[float]) -> str:
    """Return count, sum, mean, minimum and maximum."""
    if not values:
        raise ValueError("values must not be empty")
    total = sum(values)
    return f"count={len(values)} sum={total} mean={total / len(values)}"
```

`.plugin.json`：

```json
{
  "schema_version": 2,
  "id": "common",
  "tools": {
    "summarize_numbers": {
      "entrypoint": "tools/statistics.py:summarize_numbers"
    }
  }
}
```

注册名是 `common__summarize_numbers`。本地 ID 必须匹配 `[a-z][a-z0-9_]*`。模型调用时使用这个全名。

入口格式是 `相对路径.py:导出名`。导出名必须是合法标识符。加载被限制在插件根目录内。同一次编译里，同一个文件只导入一次。

普通函数会被包成工具。若导出的已经是 `BaseTool`，则沿用它，并把 `name` 改成全名。

## 访问模型和语义客户端

需要另一个已配置模型时，增加 `ToolRuntime` 参数。不要在工具里读 `.env`。

```python
from langchain.tools import ToolRuntime
from dataagent import ModelContext

async def summarize(text: str, runtime: ToolRuntime[ModelContext]) -> str:
    """Summarize text with the configured fast model."""
    model = runtime.context.get_model("fast")
    return (await model.ainvoke(text, config=runtime.config)).text
```

`get_model("fast")` 读取 `models.fast`。未知别名失败。客户端在当前 Agent 内复用。

语义服务配置存在时，`runtime.context.semantic_client` 是宿主或 SDK 拥有的 `SemanticServiceClient`。未配置时它是 `None`。用法见 [语义服务](semantic-service.md)。完整示例见 [llm-utils](../../examples/plugins/llm-utils/README.md)。

## 运行限制

每个 Agent 每次调用默认最多 128 次工具调用，超限抛错。主 Agent 和子代理分开计数，新的一轮重新计数。

`ToolException` 会变成一条可纠正的工具消息：`Invalid tool input...`。其他异常按原生工具错误处理。

工具和 shell 能访问宿主进程允许的路径。会话目录只组织文件，不隔离访问。见 [安全边界](../architecture/security-boundaries.md)。
