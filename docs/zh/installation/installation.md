## 安装部署

DataAgent 当前要求 Python `>=3.11`，推荐使用 `uv` 管理环境和依赖。

## 1. 安装 uv

如果本机还没有 `uv`，可按官方方式安装，或使用 pip：

```bash
python3 -m pip install uv
```

确认安装：

```bash
uv --version
```

## 2. 源码安装

```bash
git clone https://github.com/datagallery-ai/DataAgent.git
cd dataagent
uv sync
```

如果需要运行测试或构建文档：

```bash
uv sync --extra test
uv sync --extra mkdoc
```

开发工具：

```bash
uv sync --group dev
```

## 3. 配置环境变量

复制 `.env.example` 为 `.env`，并填写模型密钥、模型服务地址、数据库服务地址等环境差异项。

```bash
cp .env.example .env
```

具体需要哪些变量取决于你使用的 YAML 配置。例如 `MODEL` 中使用 `provider: "bailian"` 时，需要提供对应 provider 的 API Key。

## 4. 验证安装

```bash
uv run -m dataagent quickstart
```

也可以运行一个示例配置：

```python
import asyncio
from dataagent.interface.sdk.agent import DataAgent


async def main():
    agent = DataAgent.from_config("dataagent/core/flex/examples/ecommerce_agent.yaml")
    result = await agent.chat("请介绍一下你能做什么")
    print(result["messages"][-1].content)


asyncio.run(main())
```

## 5. 可选数据库服务

部分场景会使用 Elasticsearch、PostgreSQL、MySQL 或业务数据库。只有当你的配置中启用了 `MEMORY`、`DATASOURCE`、NL2SQL 数据库或特定业务工具时，才需要部署这些外部服务。

数据库服务部署与数据导入请参考：[数据库安装指导](../installation_doc/database_install/database_install.md)。

## 6. 构建分发包

如需构建 wheel/sdist：

```bash
uv build
```

生成产物位于 `dist/`。
