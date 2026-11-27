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
  G --> H[PreparedRuntime]
```

## Home

CLI 启用了 `init_home`。SDK 默认只读，需写入模板时显式传入 `LaunchOptions(init_home=True)`。初始化保留已有文件，步骤和目录见 [Home 与工作区](home-layout.md)。使用显式 `--config` 时无需写入 Home 配置模板。

## 环境与配置

启动阶段分别确定环境文件和产品配置文件，应用配置层，再从合并结果准备插件与输入绑定。文件选择和覆盖顺序见[环境变量参考](../../zh/reference/cli-and-environment.md#environment-variables)和[配置合并](../../zh/reference/configuration.md#configuration-merge)。

默认模型的 `$env{...}` 在合并之后展开。展开所用的环境快照只保留各模型字段里引用到、且当时已存在的变量名。

## 输入

默认输入目录来自所选 YAML 的 `dataagent.input_workspaces`。恢复会话时，直接使用 `sessions.sqlite` 中该会话已保存的输入绑定。

每个路径必须是可读目录。名称和路径都不能重复。

## 扩展位置

`locate_extensions` 收集：

- 包内 builtin 插件目录和 Home `plugins/` 下的插件目录
- Home `skills/` 作为来源 `User`
- 每一层配置里的 Hook，并记下该层目录

它不导入 Python。MCP 连接在下一步从 Home 和已启用插件根读取，环境引用在这时展开。连接值放进 PreparedRuntime，不在准备阶段拨号。

`StartupReport` 记录哪些配置文件被应用、插件来源和显式 env 文件路径。它不保存配置正文。

## 刷新

`refresh_extensions` 按 PreparedRuntime 记下的 Home、显式配置和环境文件读取扩展设置，返回候选运行时。候选装配成功后由宿主发布，范围和流程见[扩展生命周期](extension-lifecycle.md#refresh-and-cache)。
