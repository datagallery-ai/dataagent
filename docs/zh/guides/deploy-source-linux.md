# Linux 一键部署

在 Linux 上用 `deploy.sh` 安装依赖、构建 Web 和 TUI，并在后台运行 Web 与 Server。需要自己管理依赖或进程时，按[手动源码部署](deploy-source-manual.md)操作。

<a name="linux-prerequisites"></a>

## 准备环境

需要 Git、uv、Node.js 22.12.0 或更高版本及 npm。后端支持 Python 3.11 和 3.12。若 Node.js 缺失或版本过低，脚本可在确认后用 `apt-get`、`dnf` 或 `yum` 安装。没有相应包管理器时请预装 Node.js。使用内部制品、索引或预装 Python 时，先按[自定义镜像部署](deploy-mirrors.md)设置下载源。

在 WSL 2 中运行时，先检查[WSL 2 资源建议](deploy-source.md#wsl2-sizing)。

<a name="linux-deploy"></a>

## 部署并验证

```bash
git clone https://gitcode.com/datagallery/dataagent.git
cd dataagent
./deploy.sh
```

部署完成后，Web 和 Server 在独立的后台进程组运行。关闭终端或按 Ctrl+C 退出日志跟随时，后台进程会继续运行。TUI 构建完成后可用 `./deploy.sh tui` 单独启动。

在仓库根目录 `.env` 填写 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`，执行 `./deploy.sh restart`。打开脚本打印的地址，默认是 [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks)。按[第一次提问](../quick-start.md#first-question)发送问题，确认收到流式回复。

<a name="managed-configuration"></a>

## 交互与配置规则

首次部署生成根目录 `.env`，并确认监听端口和客户端可达地址。Home 中缺少 `config.yaml` 时会按模板创建，已有配置时仅更新 `server.host` 和 `server.port`。YAML 无法解析时会保留原文件。部署可先完成，模型凭据在提问前配置。

受管服务已运行或根目录 `.env` 非空时，再次运行交互部署会跳过问答。重新配置端口或客户端可达地址时，使用 `--reconfigure`，脚本会先备份 `.env` 并保留已有密钥。

```bash
./deploy.sh deploy --reconfigure
```

无交互部署使用 `--non-interactive`。端口冲突或需要输入 sudo 密码时会立即失败。这两个选项互斥，均用于 `deploy`。

```bash
./deploy.sh deploy --non-interactive
```

<a name="lifecycle-commands"></a>

## 生命周期命令

```bash
./deploy.sh status    # 看进程及 API / Web 健康状态
./deploy.sh start     # 用已有环境和构建产物启动
./deploy.sh stop      # 停止受管进程组
./deploy.sh restart   # 校验配置后重启
./deploy.sh logs      # 跟随运行日志
./deploy.sh doctor    # 检查依赖、配置、端口、磁盘和健康状态
./deploy.sh tui       # API 就绪后在当前终端启动 TUI
./deploy.sh help
```

`deploy` 安装依赖并构建。重新部署会停止受管进程组，适合在维护窗口执行。`start` 和 `restart` 使用已有的 Python 环境及构建产物。缺少依赖或产物时，重新执行 `deploy`。

`deploy`、`restart` 和 `doctor` 优先取显式的 `--config`、`--web-host` 和 `--web-port`，其次沿用正在运行的受管服务配置，最后使用 Home 配置和默认 Web 地址。交互部署只询问未指定的端口。切换到同一端口的重叠监听地址时，脚本用 `ss` 检查旧监听者。其他进程占用或归属不明时，现有服务继续运行，命令报告冲突。

`restart` 在停止旧服务前校验配置。`doctor` 在已有 Python 环境时使用相同的校验逻辑，否则只检查监听配置并说明跳过的项目。要排查镜像连通性或部署目录权限，先查看 `doctor` 的结果和本次部署输出。

<a name="managed-logs"></a>

## 查看部署日志

`./deploy.sh logs` 同时跟随受管服务的日志。要按故障现象定位 Server、Web 或 TUI 的日志和请求 ID，见[故障排查](troubleshooting.md)。通过反向代理访问工作台时，见 [Web 浏览器错误上报](web.md#web-origin)。
