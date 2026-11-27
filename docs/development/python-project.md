# DataAgent Python 主包

仓库根目录的 [pyproject.toml](../../pyproject.toml) 定义 Python 发行包 `datagallery-dataagent`。源码位于 `dataagent/`，导入名和 CLI 均为 `dataagent`。Python 包包含本地运行时、HTTP/A2A 服务、公开 SDK、Home 模板和内置插件。

| 要做的事 | 入口 |
| --- | --- |
| 准备源码环境并调试 | [开发环境](setup.md) |
| 构建或验证 wheel | [发布流程](release.md) |
| 安装已交付的 wheel 和 TUI | [独立包部署](../zh/guides/deploy-packages.md) |
| 调用 Python SDK 或编写插件 | [Python SDK 与扩展](../zh/guides/integrate/index.md) |
| 查找模块与依赖方向 | [架构概览](architecture/overview.md) |

可运行的配置和插件在[示例目录](../../examples/README.md)。
