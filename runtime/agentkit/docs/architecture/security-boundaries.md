# 安全

AgentKit 在本机运行。服务地址是 `127.0.0.1`，调用方与后端位于同一台机器。下面说明身份、凭据、文件和扩展实际受到的约束。

## 身份

`--user` 和 `LaunchOptions.user_id` 选择 `runtime/users/<id>/`。ID 由字母、数字、下划线和连字符组成，最长 128 个字符。它用来划分会话目录，不是登录账号。

HTTP API 不校验访问令牌。`/healthz` 在引擎就绪时返回 Home 路径、启用插件、模型名、`dataagent.entry` 和 user id，不返回输入目录、`stateDir` 或配置正文。

会话索引把 `threadId` 绑定到创建它的 user。其他 user 不能通过 API 读取该会话或它的产物。同一台机器上能读取 Home 目录的进程，仍然可以直接打开这些文件。

## 凭据

把密钥放在环境变量或配置中的 `$env{...}`。模型密钥和语义服务密码以 `SecretStr` 保存。日志和错误消息会替换 Runtime 已知的密钥，并把消息截到 1500 字符。

Trace 会遮盖常见的 API key 和 Bearer 形态。Trace 文件里仍有系统提示、对话和工具结果。语义客户端的 Basic 用户名、密码及其 Base64 形式也参与替换。

模型客户端和语义客户端放在 `ModelContext` 中，不写入消息或 Checkpoint。

## 文件和 shell

文件工具和 shell 使用 `LocalShellBackend`，`virtual_mode` 为 false。它们可以读写后端进程有权访问的路径。默认工作目录是当前会话的产物目录，未配置时为 `<home>/outputs/<user>/<thread>/`。shell 继承后端进程的环境变量，超时、输出截断和退出码遵循该后端的行为。

系统提示要求模型把输入目录当作只读，并把新文件写到 `outputs/`。提示不改变操作系统权限。HTTP 列举产物时拒绝符号链接、`..`，并跳过 `artifacts/large_tool_results` 与 `artifacts/conversation_history`。这项检查只作用于预览接口。

新建的 Home 目录模式为 `0700`，生成的 `config.json` 和 `.env` 为 `0600`。已有文件保持原来的权限。

控制数据保留在 `runtime/`，默认产物放在 Home 的 `outputs/`。产物目录不得与 Home 重叠，只有 `<home>/outputs` 区域例外，防止产物落入扩展目录后被自动加载。`runtime/` 下的目录、Home 的 `outputs/` 及其第一层目录固定为 `0700`；会话产物目录及 `artifacts/` 保留已有权限，新建时遵循 umask。Home 默认产物区之外的自定义输出目录及其上级权限不修改。

目录分区不是沙箱。同一 OS 用户运行的 Agent 仍能按绝对路径访问控制数据；本次调整没有增加文件工具或 shell 的访问限制。未来 sandbox 应只挂载当前会话产物和只读资源，不挂载 `runtime/`、配置或凭据。见 [Home 布局](../development/home-layout.md)。

## 扩展

插件、Hook 和 MCP 的 stdio 命令在后端进程中执行。启用插件就会加载并运行其中的 Python。stdio 的 `command` 来自本机 `.mcp.json`。

Hook 可以读取状态，并用异常中止本次调用。工具 Hook 的返回值必须是 `None`，不能替换工具结果。

同步 Hook 或同步工具一旦开始执行，取消 HTTP 请求不会把它从线程中强行停掉。
