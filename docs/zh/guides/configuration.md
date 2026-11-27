# 配置操作

这里介绍修改已有配置的方法。首次设置模型请看[配置速查](../reference/configuration-api.md)。字段、默认值和合并规则见[配置字段参考](../reference/configuration.md)。

示例文件：

- 最小模板：[config.example.yaml](../../../config.example.yaml)
- 覆盖当前产品字段并带两个 Hook 的示例：[examples/config.yaml](../../../examples/config.yaml)
- nl2sql 作为整张图：[examples/nl2sql-entry.yaml](../../../examples/nl2sql-entry.yaml)
- 示例字段怎么配合文件：[examples/README.md](../../../examples/README.md)

## 选择配置文件

先按[配置速查](../reference/configuration-api.md#files-to-edit)确认正在使用哪份 YAML 和环境文件。[命令与环境变量](../reference/cli-and-environment.md#environment-variables)列出了文件选择、覆盖优先级和环境变量的作用范围。

连接已有控制面时，在服务端修改相应文件。通过 `./deploy.sh` 部署时，请按[源码部署](deploy-source.md)重启服务，使 `tui`、`status`、`doctor` 使用本次启动记录的 API 地址。

<a name="优先级"></a>

## 优先级

[配置字段参考的合并规则](../reference/configuration.md#configuration-merge)规定了产品配置与插件默认值的加载顺序，以及对象、列表和 Hook 的合并方式。

某个已启用插件的 `defaults_file` 提供：

```yaml
plugins:
  config:
    demo:
      limit: 100
      format:
        header: true
        delimiter: ','
      tags:
      - default
```

所选产品文件里的对应参数为：

```yaml
plugins:
  config:
    demo:
      limit: 20
      format:
        delimiter: ;
      tags: []
```

最终传给该插件的业务参数是：

```json
{"limit":20,"format":{"header":true,"delimiter":";"},"tags":[]}
```

这些片段用于说明合并；运行时仍需完整的模型与插件声明。只写 `plugins.config.demo` 不会启用 demo。

使用相对路径时，请确认它由产品文件、插件清单还是插件自身解释。具体见[配置路径规则](../reference/configuration.md#configuration-merge)。

实现和验证依据：[启动准备](../../../dataagent/bootstrap/startup.py)、[字段合并](../../../dataagent/bootstrap/config_files.py)、[依赖解析](../../../dataagent/config/plugins.py)、[配置合并测试](../../../tests/test_plugin_configuration.py)。插件字段见 [插件](integrate/extensions/plugins.md)。

错误信息会指出本次选中的环境文件。使用多个模型时，未使用的模型可以暂缺变量。非默认模型在工具第一次调用 `get_model("别名")` 时解析环境引用，缺失变量会使那次调用失败。详见[模型字段](../reference/configuration.md#model-settings)。

改模型、服务地址、端口、Home、输入目录或 `dataagent.entry` 后，重新启动进程。插件、Skill、Hook 和 MCP 声明可以在下一轮请求前刷新，见 [扩展生命周期](../../development/architecture/extension-lifecycle.md)。`refresh_extensions` 不改 `entry`。

## 常见改动

**换默认模型。** 在 `models` 增加一个 ID，把 `dataagent.model` 改成该 ID，然后重启。没有自动回退。

**限制执行时长和循环次数。** 在 `dataagent.limits` 中分别调整超时、模型调用次数和工具调用次数。适用范围见[运行限额字段](../reference/configuration.md#dataagent)。SDK 调用方自己控制整轮时限。

**加入只读输入。** 按[输入目录指南](inputs.md)将已有目录加入 `dataagent.input_workspaces`。

**指定产物根目录。** 在配置中增加：

```yaml
dataagent:
  output_root: ~/reports
```

`output_root` 是根目录，不是某个会话的目录。新会话写入 `<output_root>/<user>/<thread>/`，例如 alice 的 task-a 写入 `~/reports/alice/task-a/`。省略或设为 `null` 时，根目录为 `<home>/outputs`，产物写入 `<home>/outputs/<user>/<thread>/`。

- 只接受绝对路径或 `~/` 开头的路径，其他相对路径报错。
- 实际位置保存在 `sessions.sqlite` 的 `session_bindings` 表。之后修改配置只影响新会话，已有会话继续使用原目录。
- 派生出的目录里已有不明来源的文件时，创建会话会报错，不会接管。
- SDK 可以用 `output_dir` 为单个任务直接指定最终目录，优先于本配置，见 [SDK 用法](integrate/sdk.md#task-outputs)。

完整规则（会话身份、独占、所有者标记、常见报错）见 [会话与产物](sessions-and-outputs.md)。

**启用插件。** 把插件目录放到 `<home>/plugins/<id>/`，或把插件自身目录写进 `plugins.paths`。再把 ID 写入 `plugins.enabled`。只加路径不会启用。示例见 [插件](integrate/extensions/plugins.md)。

**接入语义服务。** 增加 `semantic` 对象。客户端用法、认证和 TLS 见 [语义服务](integrate/extensions/semantic-service.md)。字段见配置参考。未配置 `semantic` 时，运行时不创建客户端。

**把编译型子代理当作整张图。** 在 `dataagent.entry` 写已启用插件里的编译型子代理名，然后重启。省略 `entry` 仍是 Deep Agents 根。可运行示例是 [nl2sql-entry.yaml](../../../examples/nl2sql-entry.yaml)：`entry` 为 `nl2sql`，并带上模型、sqlite 库和语义服务地址。

高层配置里的 `entry` 替换低层。Hook、调用上限、工作区、Skill、工具、MCP 工具、插件系统提示和其他子代理留在配置里，不装进这张图。`limits.timeout_seconds` 仍约束宿主执行阶段。限制见 [入口模式](integrate/extensions/subagents.md#entry-mode)。

## 写在其他文件里的设置

- MCP 连接写在 Home 或已启用插件根目录的 `.mcp.yaml`
- 工具、Skill 和子代理写在插件清单，独立 Skill 放在 `<home>/skills/<name>/SKILL.md`
- TUI 的显示选项和日志位置见[环境变量参考](../reference/cli-and-environment.md#environment-variables)

密钥放在 `.env` 或进程环境。不要把真实密钥提交到仓库。
