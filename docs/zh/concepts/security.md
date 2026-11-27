# 安全说明

控制面默认只听 `127.0.0.1`。REST 不校验访问令牌。A2A JSON-RPC 可设置 `DATAAGENT_A2A_AUTH_TOKEN` 校验共享 Bearer 令牌；Agent Card 仍公开，令牌不保护 REST，也不识别个人身份。两种入口的范围见 [REST](../reference/rest-api.md)和 [A2A](../reference/a2a.md)。

## 凭据

公开示例使用 `replace-with-your-key` 等示例值。不要把真实模型密钥、语义服务密码或 MCP 令牌写进仓库、文档或截图。

密钥放在进程环境或 Home `.env`，配置里用 `$env{...}` 引用。日志和错误消息会替换运行时已知的密钥。Trace 可能包含系统提示、对话和工具结果。分享 Trace 文件或启用远端导出前，请确认这些数据可对外提供。[Trace 处理](../../development/architecture/observability.md#trace)

密钥不要写进用户消息。客户端可提交的字段见 [AG-UI 请求契约](../reference/events.md#request)。

## 身份和文件

`--user` 只选择 `runtime/users/<id>/`。它不是登录账号。别的 user 不能通过 API 读到这个会话；能读 Home 目录的本机进程仍然可以直接打开文件。

新建的 Home 目录模式是 `0700`，初始化写出的 `config.yaml` 和 `.env` 是 `0600`。已有文件保持原权限。

工作区的只读设置通过系统提示约束 Agent。文件工具和 shell 使用控制面进程的操作系统权限，shell 从当前会话的产物目录启动。需要强制只读时，通过操作系统权限限制写入。

启用插件会在后端进程中运行插件的 Python。MCP 的 stdio 服务由后端启动为子进程。[开发者安全边界](../../development/architecture/security-boundaries.md)
