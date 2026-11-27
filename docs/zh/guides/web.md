# Web 工作台

在浏览器中提问、恢复会话和查看产物。Web 工作台与终端客户端可以连接同一个 Server。

## 启动

按[源码部署指南](deploy-source.md)准备仓库。安装依赖并配置模型后，在仓库根目录运行：

```bash
npm run build:web
npm run start
```

`npm run start` 启动 Web 并使用启动器选定地址上的本地 Server。该地址上没有控制面时会拉起后端。打开 [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks)，或使用启动器打印的 Web 地址。要改变 Web 监听端口或地址，在 `--` 后传入 `--web-port` 或 `--web-host`。

<a name="connect-server"></a>

### 只启动 Web 并连接已有 Server

未构建过 Web 的源码仓库先运行 `npm install --include=dev` 和 `npm run build:web`。Server 已运行时只需构建 Web。已有 Server 的地址与 Home `config.yaml` 一致时，运行 `npm run start:web`。如果 Server 在其他地址，在运行 Web 的机器上指定 Server URL：

```bash
API_PROXY_TARGET=http://127.0.0.1:8791 npm run start:web
```

在 PowerShell 中，先设置 `$env:API_PROXY_TARGET = 'http://127.0.0.1:8791'`，再运行 `npm run start:web`。只启动 Web 且省略 `--config` 时，启动器使用显式设置的 `API_PROXY_TARGET`。传入 `--config` 时按选中的 YAML 读取 Server 地址。两者都未指定时读取 Home `config.yaml`，默认地址为 `http://127.0.0.1:8790`。

浏览器访问同源的 `/api/v1/*`，Next.js 将请求转发到选定的 Server，并把回复流式传回浏览器。

<a name="web-origin"></a>

## 通过反向代理访问

通过反向代理、域名或通配地址访问 Web 时，在 Web 进程环境中将 `DATAAGENT_WEB_ORIGIN` 设为浏览器实际访问的 Origin，例如 `https://data.example.com`。Web 用它验证浏览器错误上报的来源，并将收到的报告写入 `web.log`。上报额度和校验规则见[日志与诊断](../../development/architecture/observability.md)。

## 提问

1. 打开 `/data-tasks`，输入并发送问题。
2. 要继续之前的对话，在会话列表中选择对应会话。要停止当前执行，使用对话区的停止按钮。
3. 打开任务控制台查看当前会话的产物。同一会话和文件也能在 [TUI](tui.md) 中查看。

模型、插件和会话输入目录由 Server 提供。需要修改设置时参阅[配置指南](configuration.md)。会话、文件预览和取消的契约见 [REST 参考](../reference/rest-api.md#sessions-and-runs)。

<a name="troubleshooting"></a>

## 排查

- 页面打不开时，确认 Web 进程仍在运行，使用启动器打印的地址。
- 代理请求返回 `502` 时，检查 Web 进程能否访问所配置的 Server。应直接在 Server 地址上请求 `/healthz` 和 `/ready`。`/ready` 返回 `503` 时，参照[启动诊断](troubleshooting.md#startup)。
- 旧面板可能请求尚未实现的资源并收到 `404`。当前支持的操作见[能力全览](../concepts/capabilities.md)。
