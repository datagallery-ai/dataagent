# 快速开始

在 Linux 上启动 DataAgent 并从 Web 工作台完成第一次提问。需要 Git、npm、uv，以及部署环境可以访问的 OpenAI 兼容模型服务。

Windows 或 macOS 用户可按[手动源码部署](guides/deploy-source.md#manual-prerequisites)准备。已拿到 Server wheel 或 TUI npm 包时，可按[独立包部署](guides/deploy-packages.md)安装。

## 1. 在 Linux 部署

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

按终端提示操作。部署成功后，脚本会打印工作台地址，Web 和 Server 在后台继续运行。环境要求和启停命令见[源码部署](guides/deploy-source.md)。

## 2. 配置模型

在仓库根目录的 `.env` 中填写模型名、API 地址和密钥：

```dotenv
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

运行 `./deploy.sh restart` 使配置生效。配置文件和字段说明见[配置](reference/configuration-api.md)。

## 3. 跑通第一个问题 { #first-question }

打开脚本打印的工作台地址，默认是 [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks)，然后发送：

```text
用三句话说明你现在能做什么。
```

收到流式回复表示 Web 已连到 Server，模型也能正常响应。想在终端试用时，运行 `./deploy.sh tui`，继续阅读 [TUI 指南](guides/tui.md)。

## 部署详情

- <a id="manual-prerequisites"></a>[准备工具和下载源](guides/deploy-source.md#manual-prerequisites)
- <a id="manual-verification"></a>[验证健康与模型调用](guides/deploy-source.md#manual-verification)
- <a id="connect-source-frontends"></a>[源码前端连接已有 Server](guides/deploy-source.md#connect-source-frontends)
- [排查源码部署](guides/deploy-source.md#deployment-troubleshooting)
