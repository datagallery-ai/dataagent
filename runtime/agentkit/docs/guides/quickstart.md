# 快速启动

完成一次本机对话。配置字段的完整表在 [配置参考](../reference/configuration.md)。

## 环境

- macOS 或 Linux
- Python 3.12
- uv
- Node.js 22+，以及仓库根目录已安装的 Node 依赖

请为 AgentKit 使用独立的 Python 环境。包的导入名是 `dataagent`。

## 安装

在仓库根目录：

```bash
uv sync --project runtime/agentkit --locked
npm install
```

## 最小配置

首次 `npm run start:tui -- --v2` 会创建 Home。默认 Home 是 `~/.dataagent`，可用进程环境变量 `DATAAGENT_HOME` 改到其他目录。

初始化只在目标文件不存在时写入模板，权限为目录 `0700`、文件 `0600`。已有文件保持原样。

编辑 `~/.dataagent/.env`：

```bash
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

模板 `config.json` 把模型名、地址和密钥写成 `$env{LLM_MODEL}`、`$env{LLM_BASE_URL}`、`$env{LLM_API_KEY}`。引用写在 JSON 字符串里，形式是 `$env{NAME}`。

也可以不初始化 Home 里的密钥，直接使用仓库根目录已有的 `.env`：

```bash
npm run start:tui -- --v2 --env-file .env
```

## 第一次运行

```bash
npm run start:tui -- --v2
```

TUI 会启动自己的 Python 后端，检查身份，然后打开对话。输入：

```text
Summarize 1, 2, 3.
```

成功时，等待期间显示 `Running…`。流结束后单独显示 `✓ Run completed · …s`。失败显示 `✗ Run failed · …s`，并保留已输出的内容和错误码。中断显示 `Interrupted — completion unknown · …s`。计时从请求发出到流结束，不含排队时间。

只启动后端：

```bash
uv run --project runtime/agentkit dataagent serve
```

就绪后可访问 `http://127.0.0.1:8790/healthz`。响应包含 `status: ready`、模型别名、Home 和 `stateDir`，不包含密钥。

## 可选输入目录

`--workspace` 指向已经存在的只读输入目录。可以重复。相对路径以启动 npm 的目录为基准。未指定时输入列表为空，启动不会创建默认输入目录。

```bash
npm run start:tui -- --v2 --env-file .env --workspace "$PWD"
```

系统提示要求模型把这些目录当作只读输入，并把生成文件写到当前会话的 `outputs/`。文件工具和 shell 使用后端进程的权限，见 [安全](../architecture/security-boundaries.md)。

## 下一步

- 改限额、插件和语义服务：[配置操作](configuration.md)
- 终端命令和已有后端：[TUI](tui.md)
- 恢复会话和查看文件：[会话与产物](sessions-and-outputs.md)
- 启动失败：[故障排查](troubleshooting.md)
