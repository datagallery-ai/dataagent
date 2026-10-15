# Hook

Hook 是一个普通 Python 函数，挂在六个生命周期点上。用异常拒绝执行。返回值不能再使用 `action: deny`。

可运行示例：[check_input.py](../../examples/hooks/check_input.py)、[check_tool.py](../../examples/hooks/check_tool.py)。

## 在配置里登记

Home 或显式配置：

```json
{
  "schema_version": 3,
  "dataagent": {
    "hooks": {
      "before_model": [
        {
          "entrypoint": "hooks/check_input.py:handle",
          "params": { "max_chars": 4000 }
        }
      ],
      "before_tool": [
        {
          "entrypoint": "hooks/check_tool.py:handle",
          "matcher": "common__summarize_numbers"
        }
      ]
    }
  }
}
```

分组里不要再写 `event`。旧的扁平列表仍然可以，每条自己带 `event`。只把文件放进 `hooks/` 不会注册。

`entrypoint` 相对声明所在的配置目录。插件和它的子代理则相对插件根。不接受绝对路径，也不接受跳出该根的路径。

`matcher` 只允许出现在 `before_tool` 和 `after_tool`。它精确匹配注册后的工具名，没有通配或正则。省略时匹配该 Agent 的全部工具。

`params` 是 JSON 值。每次调用传入一份新的深拷贝。它不是宿主配置。

## 函数签名

| 事件 | 签名 | 返回 |
| --- | --- | --- |
| `before_agent`、`after_agent`、`before_model`、`after_model` | `handle(state, runtime, *, params)` | `None`、状态更新 dict，或 LangGraph `Command` |
| `before_tool` | `handle(request, *, params)` | `None` |
| `after_tool` | `handle(request, result, *, params)` | `None` |

`params` 必须是仅关键字参数。使用普通 `def` 或 `async def`。不要用生成器，也不要传装饰后的 middleware 对象。

状态 Hook 读取 `state["messages"]` 和 `runtime.context`。返回状态更新，让原生 reducer 合并。不要就地改状态。工具 Hook 可以查看请求和结果，不能替换它们。

异步 Hook 需要 `ainvoke` 或 `astream`。同步 Hook 两种调用都可以。取消异步任务不能强行停掉已经在线程里运行的同步函数。

## 顺序和错误

声明顺序就是编译顺序。插件 Hook 在前，独立 Hook 按 Home、再显式配置追加。重复声明会执行多次，不会去重。

`before_*` 按声明顺序执行。`after_*` 按相反顺序执行。

| 事件 | 何时运行 |
| --- | --- |
| `before_agent` | 该 Agent 这次调用开始前 |
| `after_agent` | 该 Agent 正常结束时。异常或取消不会调用它 |
| `before_model` | 每次模型调用前 |
| `after_model` | 每次模型调用返回后。不是逐 token |
| `before_tool` | 工具调用前 |
| `after_tool` | 工具正常返回后，包括返回了错误 `ToolMessage`。包装调用抛出异常时不运行 |

Hook 抛出的异常会传播。运行时不吞掉、不重试，也没有单条超时。REST 的整轮 `timeout_seconds` 仍然包住整个流。

根 Agent 的 Hook 只作用于根 Agent。子代理在自己的声明里写 `hooks`。宿主的模型次数和工具次数限制对每个声明出来的 Agent 单独生效。

`command`、`timeout_seconds`、`handler` / `events` / `match`、`middleware` 和 `callbacks` 会在校验时失败。
