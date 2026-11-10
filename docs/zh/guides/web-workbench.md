# Web 工作台

网页是控制面的一种展示。它不单独保存模型、工作区或会话。

## 启动

控制面和网页一起：

```bash
npm run start
```

只起网页，并转发到已经在听的控制面：

```bash
npm run start:web
```

打开：

```text
http://127.0.0.1:3000/data-tasks
```

不要从 `/login` 开始。当前控制面没有注册和登录接口。`/api/v1/me` 直接返回进程的 `--user`。

浏览器请求同源的 `/dataagent/stream` 和 `/api/v1/*`。Next 按 `config.json` 的 `server.host` 和 `server.port` 转发，默认 `http://127.0.0.1:8790`。

就绪：

```bash
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
```

## 提问

在 `/data-tasks` 里输入问题。这一轮发到 `POST /dataagent/stream`。历史由控制面按 `threadId` 保存。模型、插件和工作区用的是控制面启动时读到的配置，不能在页面上改。

会话列表、会话内容和取消一轮都用控制面的同一条路径：`GET /sessions`、`GET /sessions/{threadId}`、`POST /runs/{runId}/cancel`。网页和 TUI 调用的路径相同；浏览器请求先到网页进程，再原样转到控制面。

页面上如果还有数据源、知识库、模型 Profile 或文件资产表单，那些请求会落到不存在的路径，返回 404。要改配置，编辑 Home `config.json` 后重启控制面。见 [配置](../reference/configuration-api.md)。

## 和 TUI 的关系

TUI 连接同一个端口，会话和产物是同一份。终端用法见 [TUI 指南](tui.md)。
