# 安全说明

这篇文档面向试用者、集成开发者和准备对外演示的维护者。读完后，你可以知道 DataFoundry 公开文档中的凭据写法、数据源连接边界和本地开发安全边界。

## 凭据写法

公开文档和示例只能使用示例值：

```text
replace-with-your-key
你的_API_Key
<dev_token>
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

## 本地开发边界

本地开发接口支持开发 token 和默认 workspace：

```text
Authorization: Bearer <dev_token>
X-Dev-Token: <dev_token>
X-Workspace-Id: default
```

不传请求头时，后端使用开发默认身份和默认 workspace。Web v1 按「一个用户拥有 default workspace」处理，不暴露 workspace 切换。自建客户端时，REST `/api/v1/*` 和 CopilotKit `/api/copilotkit` 必须发送同一组身份头，避免配置、会话、文件、产出和 run history 落到不同用户作用域。

开发期身份接口：

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/v1/me` | 读取当前用户和 workspace。 |
| GET | `/api/v1/dev/identities` | 列出本地开发用户。 |
| POST | `/api/v1/dev/users` | 创建或更新本地开发用户。 |

`/api/v1/dev/*` 生产默认禁用，除非显式设置 `DATAFOUNDRY_ENABLE_DEV_IDENTITY_API=true`。

## 密码认证模式

设置 `DATAFOUNDRY_AUTH_MODE=password` 后，后端使用基于 Cookie 的密码认证。正式态默认使用 `password`。必要配置：

```text
AUTH_SESSION_SECRET=replace-with-at-least-32-random-characters
AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000
AUTH_EMAIL_DELIVERY=test
AUTH_EMAIL_FROM=DataFoundry <no-reply@example.com>
```

正式态分两种环境（启动命令相同）：

| 环境 | `AUTH_EMAIL_DELIVERY` | `AUTH_PUBLIC_BASE_URL` |
| --- | --- | --- |
| 正式测试 | `test`（验证链接打 API 控制台） | 本机或内网地址 |
| 真实生产 | `smtp`（并配置 `AUTH_SMTP_*`） | 公网 HTTPS 域名 |

密码模式提供 `/api/v1/auth/*` 接口，用于注册、登录、邮箱验证、密码重置、退出登录、会话列表和修改密码。非安全方法请求需要携带来自 `df_csrf` Cookie 的 `X-CSRF-Token`。会话 Cookie 名为 `df_session`。

前端请同步设置 `NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=password`，并留空 `NEXT_PUBLIC_AGENT_RUNTIME_URL` / `NEXT_PUBLIC_CONFIG_API_URL`，让浏览器走同源 Next BFF；上游 API 用 `API_PROXY_TARGET`（写在 `apps/web/.env.local`）。启动命令：`npm run build && npm run build:web && npm run start:api && npm run start:web`。真实生产反代样例见 `deploy/nginx.datafoundry.conf.example`。

真实生产部署还需要 Secret 管理、审计导出、访问控制和运维监控。

## 文档发布检查

发布公开文档前，至少执行两类检查：

```bash
npm run smoke:docs
```

维护者还应在本地扫描来源敏感词、个人路径、真实凭据和发布禁用语。如果扫描命中真实敏感内容，删除内容或改成示例值。不要在公开文档里解释敏感内容的来源。
