# 启动

`prepare_runtime` 是 CLI、REST 和 SDK 共用的入口。顺序固定。

```mermaid
flowchart LR
  A[解析 Home 和 ID] --> B[可选初始化 Home]
  B --> C[加载环境]
  C --> D[合并配置]
  D --> E[决定只读输入]
  E --> F[定位扩展]
  F --> G[读取 MCP 声明]
  G --> H[Runtime]
```

## Home

`DATAAGENT_HOME` 来自进程环境。未设置时用 `~/.dataagent`。相对路径相对 `LaunchOptions.cwd`。

`init_home=True` 时创建 `config.json`、`.env`、`skills/`、`hooks/`、`plugins/`、`runtime/`。已有文件不覆盖。CLI 开启它。SDK 默认关闭。导入包、`--help` 和默认 SDK 准备都不会创建 Home。

## 环境与配置

环境文件顺序是 Home `.env`，显式配置旁边的 `.env`，然后 `--env-file`，最后进程环境覆盖。细节见 [命令与环境变量](../reference/cli-and-environment.md)。

配置层是 Home `config.json`，然后 `--config`。缺席的 Home 文件可以跳过。显式文件缺失则失败。合并规则见 [配置字段](../reference/configuration.md)。

默认模型的 `$env{...}` 在合并之后展开。展开所用的环境快照只保留各模型字段里引用到、且当时已存在的变量名。

## 输入

CLI 的 workspace 列表优先。否则看 `DATAAGENT_WORKSPACE`。否则用配置里的 `dataagent.workspaces`。恢复会话时，若调用方又给了输入，路径列表必须和 `session.json` 一致；没给则直接采用绑定。

每个路径必须是可读目录。名称和路径都不能重复。

## 扩展位置

`locate_extensions` 收集：

- 包内 builtin 插件目录和 Home `plugins/` 下的插件目录
- Home `skills/` 作为来源 `User`
- 每一层配置里的 Hook，并记下该层目录

它不导入 Python。MCP 连接在下一步从 Home 和已启用插件根读取，环境引用在这时展开。连接值放进 Runtime，不在准备阶段拨号。

`StartupReport` 记录哪些配置文件被应用、插件来源和显式 env 文件路径。它不保存配置正文。

## 刷新

`refresh_extensions` 按 Runtime 里记下的 Home、显式配置和 env 文件再走一遍扩展相关步骤。它替换 `plugins`、`dataagent.hooks`、扩展位置和 MCP。模型、限额、`server`、语义设置和路径对象保持原样。

宿主负责在发布新 Runtime 之前完成编译。见 [扩展生命周期](extension-lifecycle.md)。
