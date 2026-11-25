# 快速启动

完成一次本机对话。配置字段的完整表在 [配置参考](../reference/configuration.md)。

## 环境

- macOS 或 Linux
- uv
- Node.js 22+
- Python 3.12 和 uv

请为 DataAgent 使用独立的 Python 环境。包的导入名是 `dataagent`。

## 安装

在仓库根目录：

```bash
npm install --include=dev
uv sync --no-dev
npm run build
```

`npm install --include=dev` 准备前端依赖，`uv sync --no-dev` 准备后端运行依赖。`npm run build` 编译网页和 TUI。

## 最小配置

控制面首次启动时创建 Home。默认 Home 是 `~/.dataagent`，可用进程环境变量 `DATAAGENT_HOME` 改到其他目录。TUI 拉起后端时也会走到这次初始化。

初始化只在目标文件不存在时写入模板，权限为目录 `0700`、文件 `0600`。已有文件保持原样。

编辑 `~/.dataagent/.env`：

```bash
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

模板 `config.yaml` 把模型名、地址和密钥写成 `$env{LLM_MODEL}`、`$env{LLM_BASE_URL}`、`$env{LLM_API_KEY}`。引用写在 JSON 字符串里，形式是 `$env{NAME}`。

仓库根目录 `.env` 可以由 `npm run start` 或 `./deploy.sh start` 读取。TUI 自己拉起后端时，用 `--env-file` 把这份文件传给它。

## 第一次运行

直接启动 TUI，由它拉起后端：

```bash
npm run start:tui
```

地址来自 `config.yaml` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。控制面已经在这个地址上时，同一条命令直接连上。`--runtime-url` 只连接、不拉起。

TUI 打开对话。输入：

```text
Summarize 1, 2, 3.
```

成功时，等待期间显示 `Running…`。流结束后单独显示 `✓ Run completed · …s`。失败显示 `✗ Run failed · …s`，并保留已输出的内容和错误码。停止或连接断开显示 `Interrupted — continue this conversation · …s`。计时从请求发出到流结束，不含排队时间。

只启动后端：

```bash
uv run dataagent serve
```

就绪后可访问 `http://127.0.0.1:8790/healthz`。端口来自 `config.yaml` 的 `server.port`。响应里的对话状态在 `data.conversation`，不包含密钥。

## 可选输入目录

`--workspace` 指向已经存在的只读输入目录。可以重复。相对路径以启动 npm 的目录为基准。未指定时输入列表为空，启动不会创建默认输入目录。

TUI 拉起后端时可以重复 `--workspace`。连到已有控制面时，输入目录由那份控制面决定。工作目录由控制面的 `DATAAGENT_HOME` 决定。

系统提示要求模型把这些目录当作只读输入，并把生成文件写到当前会话的 `outputs/`。文件工具和 shell 使用后端进程的权限，见 [安全](../architecture/security-boundaries.md)。

## 下一步

- 改限额、插件和语义服务：[配置操作](configuration.md)
- 终端命令和已有后端：[TUI](tui.md)
- 恢复会话和查看文件：[会话与产物](sessions-and-outputs.md)
- 启动失败：[故障排查](troubleshooting.md)
