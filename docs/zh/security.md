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

工作台不从环境变量读取账号、邮件或数据库路径。可选的服务端模型是 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY`。控制面地址在 Home `config.json` 的 `server.host` 和 `server.port`。网页地址用 `--web-host` 和 `--web-port`，默认 `127.0.0.1:3000`。

浏览器走同源转发。启动命令：`npm run build && npm run start`。分开时用 `npm run start:api` 和 `npm run start:web`。

真实生产部署还需要 Secret 管理、审计导出、访问控制和运维监控。

## 文档发布检查

发布公开文档前，至少执行两类检查：

```bash
npm run smoke:docs
```

维护者还应在本地扫描来源敏感词、个人路径、真实凭据和发布禁用语。如果扫描命中真实敏感内容，删除内容或改成示例值。不要在公开文档里解释敏感内容的来源。
