# 安全说明

这篇文档面向试用者、集成开发者和准备对外演示的维护者。读完后，你可以知道 DataFoundry 公开文档中的凭据写法、数据源连接边界和本地开发安全边界。

## 凭据写法

公开文档和示例只能使用示例值：

```text
replace-with-your-key
你的_API_Key
```

不要把真实模型 Key、数据库密码、MCP Token、私钥、Cookie、个人访问令牌或公司内网地址写进 README、docs、issue 示例或截图。

## Agent run 边界

客户端启动 run 时，只传资源 ID 和选择信息：

- `activeDatasourceId`
- `enabledDatasourceIds`
- `enabledKnowledgeIds`
- `enabledMcpServerIds`
- `enabledSkillIds`
- `fileIds`

不要把数据库密码、模型 API Key、MCP Token 或完整连接串放进 AG-UI `messages`、`context`、`state` 或 `forwardedProps`。

## 资源配置边界

数据源、模型、MCP Server 和 Skill 的凭据只在创建或更新资源时提交。读接口返回 `secretRef`、`hasSecret` 或等价标记，不返回明文凭据。

使用 REST API 创建资源时，把凭据放在资源配置接口的字段中，不要放进自然语言问题：

```json
{
  "id": "sales-pg",
  "name": "Sales PostgreSQL",
  "type": "postgresql",
  "config": {
    "host": "127.0.0.1",
    "port": 5432,
    "database": "sales",
    "username": "readonly"
  },
  "credentials": {
    "password": "replace-with-your-key"
  }
}
```

## 数据源连接建议

- 首次接入使用只读账号或测试库。
- 给 PostgreSQL、MySQL、SQL Server、Oracle、Snowflake、BigQuery 等外部服务配置最小权限。
- 为查询设置合理的 `maxRows` 和 `timeoutMs`。
- 对邮箱、手机号、身份证号等字段配置 `maskFields`。
- 对敏感库表使用 allowlist。
- SQLite、CSV、Excel、DuckDB 文件路径必须是后端进程可访问的路径。

## 身份与会话

DataFoundry 仅支持基于 Cookie 的密码会话。开发 token、`/api/v1/dev/*` 与 `DATAFOUNDRY_AUTH_MODE` 已移除。

必要配置：

```text
AUTH_SESSION_SECRET=replace-with-at-least-32-random-characters
AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000
AUTH_REGISTRATION_MODE=open
AUTH_EMAIL_DELIVERY=test
AUTH_EMAIL_FROM=DataFoundry <no-reply@example.com>
```

`AUTH_REGISTRATION_MODE` 必填（`open` = 开放自助注册，`closed` = 注册返回 `REGISTRATION_CLOSED`）。一键部署默认 `open`；对公网暴露且不接受公开注册时请设为 `closed`。`GET /api/v1/me` 读取当前用户；`GET /api/v1/auth/status` 仅暴露 `registrationEnabled`，不含密钥。

正式态分两种环境（启动命令相同）：

| 环境 | `AUTH_EMAIL_DELIVERY` | `AUTH_PUBLIC_BASE_URL` | 典型 `AUTH_REGISTRATION_MODE` |
| --- | --- | --- | --- |
| 正式测试 | `test`（验证链接打 API 控制台） | 回环 HTTP URL | `open` |
| 真实生产 | `smtp`（并配置 `AUTH_SMTP_*`） | 公网 HTTPS 域名 | 默认 `closed`，除非明确要开放自助注册 |

`/api/v1/auth/*` 提供注册、登录、邮箱验证、密码重置、退出登录、会话列表和修改密码。非安全方法需要 `X-CSRF-Token`（来自 `df_csrf` Cookie）。会话 Cookie 为 `df_session`。Cookie 的 `Path` / `Secure` 跟随 `AUTH_PUBLIC_BASE_URL`（HTTPS ⇒ `Secure`；pathname 前缀成为 cookie path）。非回环 `AUTH_PUBLIC_BASE_URL` 时禁止 `AUTH_EMAIL_DELIVERY=test`。

前端请留空 `NEXT_PUBLIC_AGENT_RUNTIME_URL` / `NEXT_PUBLIC_CONFIG_API_URL`，让浏览器走同源 Next BFF；上游 API 用 `API_PROXY_TARGET`（写在 `apps/web/.env.local`）。启动命令：`npm run build && npm run build:web && npm run start:api && npm run start:web`。真实生产反代样例见 `deploy/nginx.datafoundry.conf.example`。

若旧 `.env` 仍含 `DATAFOUNDRY_AUTH_MODE=password`，API 会忽略该值；`./deploy.sh deploy` 会把它从配置中剥离。`DATAFOUNDRY_AUTH_MODE=dev` 会直接导致启动失败。

password-only 切换**不会**迁移仍含 `users.dev_token` 的 Metadata 库。打开此类数据库会以 `METADATA_SCHEMA_INCOMPATIBLE` 失败。请停栈后重置 `STORAGE_ROOT_DIR` / `METADATA_DB_PATH` / `MASTRA_STORAGE_PATH` / `FILE_ASSET_STORAGE_ROOT` / `WORKSPACE_ROOT`（或改指向空目录），再启动并重新注册。本次切换不提供原地 schema 升级。

真实生产部署还需要 Secret 管理、审计导出、访问控制和运维监控。

## 文档发布检查

发布公开文档前，至少执行两类检查：

```bash
npm run smoke:docs
```

维护者还应在本地扫描来源敏感词、个人路径、真实凭据和发布禁用语。如果扫描命中真实敏感内容，删除内容或改成示例值。不要在公开文档里解释敏感内容的来源。
