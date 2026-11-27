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
  plugins/<id>/
  outputs/<user>/<thread>/artifacts/
  runtime/
    backend.lock
    checkpoints.sqlite
    sessions.sqlite
    logs/
      backend.log
      web.log
      tui.log
    users/<user>/sessions/<thread>/traces/
```

Home 初始化创建缺少的 Home、`skills/`、`hooks/`、`plugins/`、`runtime/` 和配置模板。CLI 默认初始化，SDK 调用方可通过 `LaunchOptions(init_home=True)` 启用。显式指定配置文件时，不写入 Home 的 `config.yaml`。初始化以 `0700` 创建目录，以 `0600` 创建模板，并保留既有项目。会话和默认产物目录按需创建，运行时权限见[状态与存储](state-and-storage.md)。

Home 级 MCP 使用可选的 `.mcp.yaml`。已启用插件也可在各自根目录声明 MCP。Home `hooks/` 用于容纳配置通过 `entrypoint` 引用的文件。插件候选还包括包内 `dataagent/plugins/` 与显式 `plugins.paths`。资源定位和加载顺序见[启动](bootstrap.md)与[扩展生命周期](extension-lifecycle.md)。

<a name="workspaces-and-sessions"></a>

## 工作区与会话

工作区是所选 YAML 的 `dataagent.input_workspaces` 指定的输入目录。目录须存在，并且进程能读取和进入。逻辑名称不会成为路径的一部分。使用步骤见[输入与工作区](../../zh/guides/inputs.md)，恢复会话时的绑定规则见[会话与产物](../../zh/guides/sessions-and-outputs.md)。

运行状态位于 Home 的 `runtime/`，会话产物位于独立的业务目录。文件工具和 shell 使用宿主进程拥有的 OS 权限，默认工作目录为会话产物目录。[安全边界](security-boundaries.md)说明应用层路径校验与系统权限各自的作用。
