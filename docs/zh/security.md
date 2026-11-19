# 安全说明

控制面默认只听 `127.0.0.1`。REST 不校验访问令牌。A2A JSON-RPC 可设置 `DATAAGENT_A2A_AUTH_TOKEN` 校验共享 Bearer 令牌；Agent Card 仍公开，令牌不保护 REST，也不识别个人身份。两种入口的范围见 [REST 与 A2A](reference/rest-api.md)。

## 凭据

公开示例使用 `replace-with-your-key` 等示例值。不要把真实模型密钥、语义服务密码或 MCP 令牌写进仓库、文档或截图。

密钥放在进程环境或 Home `.env`，配置里用 `$env{...}` 引用。日志和错误消息会替换运行时已知的密钥。Trace 里仍有系统提示、对话和工具结果。

`POST /api/v1/runs` 要求 `context: []`、`forwardedProps: {}`，`state` 只能省略、为 `null` 或 `{}`，不接受客户端注入非空内容。不要把密钥写进用户消息。

## 身份和文件

`--user` 只选择 `runtime/users/<id>/`。它不是登录账号。别的 user 不能通过 API 读到这个会话；能读 Home 目录的本机进程仍然可以直接打开文件。

新建的 Home 目录模式是 `0700`，初始化写出的 `config.yaml` 和 `.env` 是 `0600`。已有文件保持原权限。

工作区在配置里标记为只读。系统提示要求模型把新文件写到会话 `outputs/`。提示不改变操作系统权限。文件工具和 shell 使用控制面进程自己的权限，默认工作目录是该会话的 `outputs/`。

启用插件就会在这个进程里加载并运行其中的 Python。MCP 的 stdio 命令也在这个进程里执行。
