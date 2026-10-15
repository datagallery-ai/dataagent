# 安全

AgentKit 在本机运行。服务地址是 `127.0.0.1`，调用方与后端位于同一台机器。下面说明身份、凭据、文件和扩展实际受到的约束。

## 身份

`--user` 和 `LaunchOptions.user_id` 选择 `runtime/users/<id>/`。ID 由字母、数字、下划线和连字符组成，最长 128 个字符。它用来划分会话目录，不是登录账号。

HTTP API 不校验访问令牌。`/healthz` 返回 Home、输入目录和状态目录的路径，不返回配置正文或密钥。

会话索引把 `threadId` 绑定到创建它的 user。其他 user 不能通过 API 读取该会话或它的产物。同一台机器上能读取 Home 目录的进程，仍然可以直接打开这些文件。

## 凭据

把密钥放在环境变量或配置中的 `$env{...}`。模型密钥和语义服务密码以 `SecretStr` 保存。日志和错误消息会替换 Runtime 已知的密钥，并把消息截到 1500 字符。

Trace 会遮盖常见的 API key 和 Bearer 形态。Trace 文件里仍有系统提示、对话和工具结果。语义客户端的 Basic 用户名、密码及其 Base64 形式也参与替换。

模型客户端和语义客户端放在 `ModelContext` 中，不写入消息或 Checkpoint。

## 文件和 shell

文件工具和 shell 使用 `LocalShellBackend`，`virtual_mode` 为 false。它们可以读写后端进程有权访问的路径。默认工作目录是当前会话的 `outputs/`。shell 继承后端进程的环境变量，超时、输出截断和退出码遵循该后端的行为。

系统提示要求模型把输入目录当作只读，并把新文件写到 `outputs/`。提示不改变操作系统权限。HTTP 列举产物时拒绝符号链接、`..`，并跳过 `artifacts/large_tool_results` 与 `artifacts/conversation_history`。这项检查只作用于预览接口。

新建的 Home 目录模式为 `0700`，生成的 `config.json` 和 `.env` 为 `0600`。已有文件保持原来的权限。

## 扩展

插件、Hook 和 MCP 的 stdio 命令在后端进程中执行。启用插件就会加载并运行其中的 Python。stdio 的 `command` 来自本机 `.mcp.json`。

Hook 可以读取状态，并用异常中止本次调用。工具 Hook 的返回值必须是 `None`，不能替换工具结果。

同步 Hook 或同步工具一旦开始执行，取消 HTTP 请求不会把它从线程中强行停掉。
