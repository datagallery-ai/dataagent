# 安全边界实现

面向部署者的网络、身份和文件权限契约见 [安全说明](../../zh/concepts/security.md)。本页记录控制面和运行时落实这些边界的位置。

## 请求与会话 { #request-and-session }

[产品路由](../../../dataagent/server/product.py)在引擎就绪时通过 `/healthz` 返回 Home 路径、启用插件、模型名、`dataagent.entry` 和用户 ID。请求与响应字段见 [REST API](../../zh/reference/rest-api.md#health-and-identity)。

`--user` 和 `LaunchOptions.user_id` 选择宿主本地的会话范围，ID 使用 `OPAQUE_ID_PATTERN`，首字符须为 ASCII 字母或数字，总长 1–128。会话索引和目录绑定一起校验用户归属。A2A 的 Bearer 校验仅挂在 JSON-RPC 路径。 [会话隔离测试](../../../tests/test_session_isolation.py) · [A2A 鉴权测试](../../../tests/test_a2a_server.py)

## 凭据与诊断 { #credentials-and-diagnostics }

模型密钥和语义服务密码使用 `SecretStr`。模型客户端和语义客户端由 `AgentContext` 的私有属性持有。凭据字段匹配、已知密钥替换、日志和 Trace 的数据范围见 [可观测性](observability.md)。

## 文件与进程 { #files-and-processes }

[文件系统装配](../../../dataagent/extensions/filesystem_backend.py)使用 `LocalShellBackend(virtual_mode=False, inherit_env=True)`。shell 以会话产物目录为工作目录，继承后端进程环境。文件工具和 shell 依赖宿主操作系统的权限，输入只读由系统提示表达。[文件访问边界测试](../../../tests/test_agent.py)

[产物路由](../../../dataagent/server/outputs.py)的列表只返回普通文件，跳过符号链接及 `artifacts/large_tool_results`、`artifacts/conversation_history`。预览还验证请求路径，拒绝绝对路径、`..` 或 NUL，并拒绝经过符号链接的路径。这些约束作用于 HTTP 产物读取。 [产物接口测试](../../../tests/test_outputs.py)

Home 权限、产物路径重叠校验与会话绑定见 [Home 与工作区](home-layout.md)和[状态与存储](state-and-storage.md)。

## 扩展执行 { #extension-execution }

插件与 Hook 的 Python 在宿主进程中执行。MCP 的 stdio 服务由宿主按 `.mcp.yaml` 启动为子进程。 [MCP 配置参考](../../zh/reference/mcp-configuration.md)

工具 Hook 可以替换请求、短路调用或替换工具结果，返回关联 ID 的规则见 [Hook 函数签名](../../zh/guides/integrate/extensions/hooks.md#hook-signatures)。同步 Hook 或同步工具一旦开始执行，取消 HTTP 请求不会从线程中强制停止它。
