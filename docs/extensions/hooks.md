# Hook

Hook 是一个普通 Python 函数，挂在六个生命周期点上。拒绝有两种方式，见下文“拒绝执行”。

可运行示例：[check_input.py](../../examples/hooks/check_input.py)、[check_tool.py](../../examples/hooks/check_tool.py)。

## 在配置里登记

登记在选中的产品文件里。未传 `--config` 时是 Home `config.yaml`，传了就只用那份文件：

```yaml
schema_version: 4
dataagent:
  hooks:
    before_model:
    - entrypoint: hooks/check_input.py:handle
      params:
        max_chars: 4000
    before_tool:
    - entrypoint: hooks/check_tool.py:handle
      matcher: common__summarize_numbers
```

分组里不要再写 `event`。旧的扁平列表仍然可以，每条自己带 `event`。只把文件放进 `hooks/` 不会注册。

`entrypoint` 相对声明所在的配置目录。插件和它的子代理则相对插件根。不接受绝对路径，也不接受跳出该根的路径。

`matcher` 只允许出现在 `before_tool` 和 `after_tool`。它精确匹配注册后的工具名，没有通配或正则。省略时匹配该 Agent 的全部工具。

`params` 是 JSON 值。每次调用传入一份新的深拷贝。它不是宿主配置。

## 函数签名

| 事件 | 签名 | 返回 |
| --- | --- | --- |
| `before_agent`、`after_agent`、`before_model`、`after_model` | `handle(state, runtime, *, params)` | `None`、状态更新 dict，或 LangGraph `Command` |
| `before_tool` | `handle(request, *, params)` | `None`、新的 `ToolCallRequest`，或直接返回 `ToolMessage`/`Command` |
| `after_tool` | `handle(request, result, *, params)` | `None`、原生 `ToolMessage` 或 `Command` |

`params` 必须是仅关键字参数。使用普通 `def` 或 `async def`。不要用生成器，也不要传装饰后的 middleware 对象。

状态 Hook 读取 `state["messages"]` 和 `runtime.context`。工具 Hook 没有 `runtime` 参数，从 `request.runtime.context` 取同一个上下文（模型、语义客户端、插件配置）。返回状态更新，让原生 reducer 合并。不要就地改状态。工具 Hook 可以返回替换对象；None 保留原对象。前置请求替换保留 tool_call_id；前置直接返回结果时跳过工具执行。
结果替换必须含对应 tool_call_id 的 ToolMessage。
替换 Command 时须保留需要的其他状态更新；推荐通过 dataclasses.replace(result, update=...) 构造。

异步 Hook 需要 `ainvoke` 或 `astream`。同步 Hook 两种调用都可以。取消异步任务不能强行停掉已经在线程里运行的同步函数。

## 顺序和错误

声明顺序就是编译顺序。已启用插件清单里的 Hook 在前，然后是选中产品文件里的 `dataagent.hooks`。重复声明会执行多次，不会去重。

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

## 拒绝执行

| 方式 | 效果 | 适用 |
| --- | --- | --- |
| `before_tool` 返回 `ToolMessage(content=..., tool_call_id=request.tool_call["id"], status="error")` | 只拒绝这一次工具调用。工具不执行，模型看到这条消息，可以改正后继续 | 参数不合规、权限不足等模型能纠正的情况 |
| 任何 Hook 抛出异常 | 结束整轮。HTTP 客户端收到 `RUN_ERROR`，`code` 通常是 `INTERNAL_ERROR` | 继续运行没有意义或不安全 |

Hook 位于工具错误处理之外，所以 Hook 里的异常不会被转换成工具结果。

根 Agent 的 Hook 只作用于根 Agent。子代理在自己的声明里写 `hooks`。宿主的模型次数和工具次数限制对每个声明出来的 Agent 单独生效。

## 编译工作流的输入与结果 Hook

编译子代理在自己的工厂中使用 `services.run_hooks`。它们与上面六种 Agent 生命周期事件分别声明，
当前内置使用方是 NL2SQL。默认配置片段可以给依赖的工作流登记：

```yaml
plugins:
  config:
    nl2sql:
      run_hooks:
        before_run:
        - entrypoint: hooks/nl2sql.py:prepare_input
        after_run:
        - entrypoint: hooks/nl2sql.py:finish_output
```

入口相对贡献这段配置的插件根或用户配置目录。函数签名为
`handle(value, runtime, *, params)`；`runtime.context` 提供宿主管理的模型、语义客户端、插件配置和会话路径。
运行前返回的字典覆盖当次输入中的同名字段；运行后返回完整结果字典。返回 `None` 保留原值，
参数和值均按次深拷贝。前置按声明顺序执行，后置逆序；不能用后置返回值抹掉已有错误。

NL2SQL 在 Perceptor 之前执行输入 Hook，在工作流正常返回后执行结果 Hook。它保留显式注入的空值，
避免重新加载默认 Schema 或业务证据。可覆盖字段与错误约定见
[编译工作流 Hook](../reference/extension-schema.md#编译工作流-hook)；
实际使用例见 [de-agent](../../dataagent/plugins/de-agent/README.md)。
