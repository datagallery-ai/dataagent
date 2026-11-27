# Home、工作区与目录

运行时使用一个 Home。默认是 `~/.dataagent`，可通过进程环境的 `DATAAGENT_HOME` 选择其他位置。相对路径以启动时的工作目录解析。配置和环境文件的选择规则见[配置参考](../../zh/reference/configuration.md)与[环境变量](../../zh/reference/cli-and-environment.md#environment-variables)。

<a name="home-layout"></a>

## 目录布局

```text
<home>/
  config.yaml
  .env
  .mcp.yaml
  skills/<name>/SKILL.md
  hooks/
  plugins/<id>/.plugin.yaml
  outputs/<user>/<thread>/
    artifacts/
      conversation_history/
      large_tool_results/
  runtime/
    backend.lock
    checkpoints.sqlite
    sessions.sqlite
    logs/
      backend.log
      web.log
      tui.log
    users/<user>/sessions/<thread>/traces/<native-root-run-id>.json
```

| 路径 | 放什么 | 何时使用 |
| --- | --- | --- |
| `config.yaml` | 模型、默认输入目录、服务设置和启用的插件 | 启动时读取。显式指定配置文件可替换这份文件 |
| `.env` | 配置中通过 `$env{NAME}` 引用的值，例如模型凭据 | 启动时选用的默认环境文件 |
| `.mcp.yaml` | Home 级的可选 MCP 连接定义 | 发现 MCP 工具时读取，已启用的插件也可在自己的根目录声明连接 |
| `skills/<name>/SKILL.md` | 独立 Skill 的说明和元数据 | 扩展装配时按 Skill 规则发现 |
| `hooks/` | 配置中 `entrypoint` 指向的 Hook 源码 | 按声明加载 |
| `plugins/<id>/.plugin.yaml` | 声明插件工具、子代理、Skill 和 Hook | 启用相应插件后加载。包内插件与 `plugins.paths` 是另外的来源 |
| `outputs/<user>/<thread>/` | 默认业务文件和 `artifacts/` 中卸载的历史、工具结果 | 首次登记会话时建立产物目录，卸载子目录按需写入 |
| `runtime/backend.lock` | 同一 Home 的服务进程排他锁 | 启动时尝试获取。仅看文件是否存在无法判断服务是否在运行 |
| `runtime/checkpoints.sqlite` | 服务端 Agent 的消息与图状态 | 建立服务端 checkpointer 后写入 |
| `runtime/sessions.sqlite` | 会话列表、状态、输入目录和产物归属 | 服务端持久化会话索引，SDK 也在其中登记输入与产物绑定 |
| `runtime/logs/{backend,web,tui}.log` | 后端、网页、终端各自的运行诊断 | 对应进程写入并按[日志规则](observability.md)轮转 |
| `runtime/users/<user>/sessions/<thread>/traces/` | 每次根执行的 OTLP JSON 文件 | 根执行收尾时导出，详见[执行 Trace](execution-traces.md) |

CLI 默认初始化缺少的 Home 目录和模板。SDK 调用方可通过 `LaunchOptions(init_home=True)` 启用初始化。显式指定配置文件时，Home 初始化跳过 `config.yaml`。新目录以 `0700` 创建，模板以 `0600` 创建，既有文件原样保留。运行时目录和产物目录的权限规则见[状态与存储](state-and-storage.md#permissions-and-ownership)。

资源定位和加载顺序见[启动](bootstrap.md)与[扩展生命周期](extension-lifecycle.md)。

<a name="workspaces-and-sessions"></a>

## 工作区与会话

工作区是所选 YAML 的 `dataagent.input_workspaces` 指定的输入目录。目录须存在，并且进程能读取和进入。逻辑名称不会成为路径的一部分。使用步骤见[输入与工作区](../../zh/guides/inputs.md)，恢复会话时的绑定规则见[会话与产物](../../zh/guides/sessions-and-outputs.md)。

运行状态位于 Home 的 `runtime/`，会话产物位于独立的业务目录。文件工具和 shell 使用宿主进程拥有的 OS 权限，默认工作目录为会话产物目录。[安全边界](security-boundaries.md)说明应用层路径校验与系统权限各自的作用。
