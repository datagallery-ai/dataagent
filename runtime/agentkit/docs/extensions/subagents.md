# 子代理

子代理是插件清单 `subagents` 引用的一份 JSON。模型通过内置 `task` 工具把子任务交给它。

示例：[general-purpose.json](../../examples/plugins/common/subagents/general-purpose.json)。

## 最小声明

```json
{
  "name": "general-purpose",
  "description": "Handle focused research and file inspection.",
  "system_prompt": "prompts/general-purpose.md"
}
```

`name` 匹配 `[a-z][a-z0-9_-]*`。`description` 不能空。`system_prompt` 是相对插件根的文件。名称不能和根名 `dataagent-v2` 或其他子代理重复。

## 继承

| 字段 | 省略时 | 写出来时 |
| --- | --- | --- |
| `tools` | 使用当前已注册的全部工具 | 只使用列出的工具。短名补成 `<plugin-id>__<短名>`；已含 `__` 的名字原样解析。未知或重复引用会使编译失败 |
| `skills` | 继承根 Agent 的 Skill 来源 | 按路径重新编译。`[]` 表示不加载 |
| `hooks` | 没有 Hook | 只作用于这个子代理，语法与根 Hook 相同 |

`"tools": []` 表示不附加声明工具。省略和空列表是两种结果。

子代理使用与根 Agent 相同的模型配置。它有自己的系统提示，并附上同一份文件系统说明。

## 执行限制

`dataagent.limits` 对每个 Agent 每次调用单独计算：

- `model_calls_per_agent` 默认 128。这是该 Agent 的模型循环上限，不是整棵子树的预算，也不把一次 HTTP 重试算成多次循环。
- `tool_calls_per_agent` 默认 128。超限抛错。新一轮重新计数。

根 Agent 会装上待办列表中间件。子代理不装。

未声明名为 `general-purpose` 的子代理时，运行时会加入内置的 general-purpose。它使用父级工具和 Skill。插件中为其他子代理声明的 Hook 不会套用到它。若要为这个名字配置 Hook 和限额，请在插件中声明同名子代理；声明之后使用插件中的定义。

子代理可见性在 AG-UI 适配器里是 `hidden`。客户端看到的是根运行的事件，不把子代理运行展开成独立的用户回合。
