<a name="_1"></a>

# 源码部署

源码从 [DataAgent 的 GitCode 仓库](https://gitcode.com/datagallery/dataagent)获取。选择适合部署环境的一条路径。

| 环境 | 指南 |
| --- | --- |
| Linux，由脚本安装依赖、构建并托管 Web 和 Server | [Linux 一键部署](deploy-source-linux.md) |
| Windows、macOS，或自行管理 Linux 依赖和进程 | [手动源码部署](deploy-source-manual.md) |
| 已有 Server wheel 或 TUI npm 包，或只想安装交付制品 | [独立包部署](deploy-packages.md) |
| 下载受限，需要内部镜像或预装解释器 | [自定义镜像部署](deploy-mirrors.md) |

<a name="_2"></a>
<a name="环境要求"></a>

<a name="source-requirements"></a>

## 部署前准备

准备 Git、Node.js 22.12.0 或更高版本、npm、uv，以及可从 Server 访问的 OpenAI 兼容模型服务。后端支持 Python 3.11 和 3.12。Linux 一键部署可在确认后通过系统包管理器安装缺失的 Node.js。具体做法见对应部署路径。

DataAgent 本身通过源码或独立包部署。当前仓库未随 DataAgent 发布 Docker 或 Compose 部署定义。控制面没有注册登录。配置服务访问方式前请阅读[安全边界](../concepts/security.md)。

<a name="wsl2-sizing"></a>

### WSL 2 资源建议

以下配置用于在 WSL 2 中从源码安装、构建并启动 Server、Web 和一个 TUI。CPU 和内存指分配给 WSL 2 的资源，磁盘指安装前应预留的可用空间。

| 场景 | 逻辑 CPU | 内存 | 可用磁盘 |
| --- | ---: | ---: | ---: |
| 单人使用的最低配置建议 | 4 | 6 GiB | 15 GiB |
| 日常开发与反复构建 | 8 | 10–12 GiB | 25 GiB |

在 WSL 的 Linux 文件系统中存放仓库和依赖，例如 `~/dataagent`，以减少构建时的跨文件系统 I/O。磁盘预算包含依赖缓存和构建余量，业务数据、长期日志和会话记录需另计。以上配置按远程模型服务和轻量数据任务估算，本地模型、Spark 或大数据任务需按负载增配。

Windows 可通过 `%UserProfile%\.wslconfig` 配置 `processors`、`memory` 和 `swap`。设置上限时也要为 Windows 和并行使用的其他应用预留内存。详见 [WSL 文件系统建议](https://learn.microsoft.com/en-us/windows/wsl/filesystems)和 [WSL 2 配置说明](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)。

<a name="previous-sections"></a>

## 原章节对应位置

| 原章节 | 当前文档 |
| --- | --- |
| <a name="推荐-linux-一键部署"></a><a name="linux"></a><a name="推荐linux-一键部署"></a>Linux 一键部署 | [部署并验证](deploy-source-linux.md#linux-deploy) |
| <a name="交互与配置规则"></a><a name="_3"></a>交互与配置规则 | [端口和配置规则](deploy-source-linux.md#managed-configuration) |
| <a name="生命周期命令"></a><a name="_4"></a>生命周期命令 | [启停与状态](deploy-source-linux.md#lifecycle-commands) |
| <a name="日志位置"></a><a name="_5"></a>日志位置 | [查看部署日志](deploy-source-linux.md#managed-logs) |
| <a name="windows-macos-其他-手动-npm-部署"></a><a name="windows-macos-npm"></a><a name="windows--macos--其他手动-npm-部署"></a>Windows / macOS / 其他手动部署 | [手动源码部署](deploy-source-manual.md) |
| <a name="manual-prerequisites"></a><a name="1-准备工具和下载源"></a><a name="1"></a>准备工具和下载源 | [工具与下载源](deploy-source-manual.md#manual-prerequisites) |
| <a name="2-安装依赖"></a><a name="2"></a>安装依赖 | [安装依赖](deploy-source-manual.md#manual-dependencies) |
| <a name="3-配置模型服务"></a><a name="3"></a>配置模型服务 | [模型服务配置](deploy-source-manual.md#manual-model) |
| <a name="4-构建并启动-正式测试-真实生产相同"></a><a name="4"></a><a name="4-构建并启动正式测试--真实生产相同"></a>构建并启动 | [构建与启动](deploy-source-manual.md#manual-start) |
| <a name="manual-verification"></a><a name="5-验证健康与模型调用"></a><a name="5"></a>验证健康与模型调用 | [健康与模型验证](deploy-source-manual.md#manual-verification) |
| <a name="connect-source-frontends"></a><a name="源码前端连接已有-server"></a><a name="server"></a>源码前端连接已有 Server | [Web 连接](web.md#connect-server)、[TUI 连接](tui.md#connect-server) |
| <a name="源码-server-使用编译型插件入口"></a><a name="server_1"></a>使用编译型插件入口 | [编译型子代理与入口模式](integrate/extensions/subagents.md#entry-mode) |
| <a name="启动-tui"></a><a name="tui"></a>启动 TUI | [TUI 指南](tui.md) |
| <a name="deployment-troubleshooting"></a><a name="排查"></a><a name="troubleshooting"></a>部署排查 | [故障排查](troubleshooting.md) |
| <a name="node-版本不对"></a><a name="node"></a>Node 版本不对 | [Node.js 前置条件](deploy-source-manual.md#manual-prerequisites) |
| <a name="页面打不开"></a><a name="_6"></a>页面打不开 | [Web 排查](web.md#troubleshooting) |
| <a name="后端未启动"></a><a name="_7"></a>后端未启动 | [启动诊断](troubleshooting.md#startup) |
| <a name="模型不可用"></a><a name="_8"></a>模型不可用 | [模型诊断](troubleshooting.md#models) |
| <a name="端口冲突"></a><a name="_9"></a>端口冲突 | [端口与启动诊断](troubleshooting.md#startup) |
| <a name="模型读不到本机目录"></a><a name="_10"></a>模型读不到本机目录 | [只读工作区](inputs.md#read-only-workspaces) |
| <a name="下一步"></a><a name="_11"></a>下一步 | [完成第一次提问](../quick-start.md) |
