# 构建与发布

当前交付两个独立制品：Server wheel 和 TUI npm 包。Web 从源码构建并以当前 checkout 中的产物运行。已拿到制品时按[独立包部署](../zh/guides/deploy-packages.md)安装。

<a name="build-artifacts"></a>

## 构建制品

需要 Python 3.11 或 3.12、uv 和 Node.js ≥22.12.0。在仓库根目录运行：

```bash
make prepare PYTHON_VERSION=3.12
make build PYTHON_VERSION=3.12
```

`make prepare` 安装 npm 依赖并同步 Python 运行依赖。`make build` 调用 uv 构建 Server wheel，编译 Web 和 TUI，再由 [package-tui.mjs](../../scripts/package-tui.mjs) 打包 TUI。Web 项目源码部署时可单独执行 `npm run build:web`。

| 输出 | 内容与元数据 |
| --- | --- |
| `dist/server/*.whl` | Python 发行包 `datagallery-dataagent`。导入名和 CLI 为 `dataagent`，包内容和版本见 [pyproject.toml](../../pyproject.toml) |
| `dist/tui/*.tgz` | npm 包 `@datagallery/tui`，入口为 `dataagent-cli` 和 `da`。版本见 [ui/tui/package.json](../../ui/tui/package.json) |
| `ui/web/.next/` | Web 构建结果，留在源码目录运行 |

只出 Server wheel 时，从仓库根运行：

```bash
uv build --wheel --clear --out-dir dist/server
```

构建命令只生成文件，上传和分发由发布作业另行处理。记录对应的 Git 提交、版本和校验值，交付本次验收实际使用的文件。

<a name="verify-artifacts"></a>

## 验收制品

```bash
make test
uv run --no-sync ruff check .
npm run smoke:dataagent-package
```

`make test` 的测试层级与凭据使用说明见[测试指南](testing.md)。[制品 smoke](../../scripts/smoke-dataagent-package.mjs) 使用已构建的 Web，并在仓库外安装本次 Server wheel 和 TUI tgz。它检查 CLI、内置资源、SDK、HTTP 和 A2A，并通过本地模型替身验证 Web 代理、TUI 连接与流式结果。

| 场景 | 预期 |
| --- | --- |
| 模型配置齐全 | 请求流收到回复并以完成事件结束 |
| 上游模型调用失败 | AG-UI 以 `RUN_ERROR` 结束 |
| Server 缺少模型配置 | `/healthz` 返回 200，`/ready` 和对话接口返回 503 |
| Server 已停止 | Web 代理返回 502，TUI 报连接失败 |

Smoke 使用本机模型替身。真实模型和数据库连接按交付环境另外验收。Python sdist、wheel 文件与安装资源可用[安装包测试](testing.md#package-tests)单独检查。

<a name="compatibility-review"></a>

## 兼容性检查

- 修改公开配置时依据[产品配置](../zh/reference/configuration.md)、[扩展声明](../zh/reference/extension-schema.md)和 [MCP 配置](../zh/reference/mcp-configuration.md)检查版本与字段。
- 修改图状态或私有状态键时，用升级前的会话数据验证仍能读取 Checkpoint 并按约定续接。会话索引、绑定、产物和 Trace 的归属见[状态与存储](architecture/state-and-storage.md)。
- 修改长工具结果的目录时，检查历史消息中的引用能否打开。相关落盘规则见[上下文管理](architecture/context-management.md)。
