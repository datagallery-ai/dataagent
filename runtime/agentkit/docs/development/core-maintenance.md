# 内置 core

`runtime/agentkit/dataagent/core` 随仓库发布，安装时不再下载另一份 Deep Agents 包。产品通过 `dataagent.core.graph.create_deep_agent` 装配图。

模型能看到的工具说明和子代理说明来自 [deepagents](https://github.com/langchain-ai/deepagents)，版权归 LangChain, Inc.，许可为 MIT。文件头保留这段署名。这些句子决定模型如何使用工具。修改原文会改变运行行为；跟随上游更新时，按上游原文整段替换。

子代理状态键与 deepagents 0.7.15 对齐。仓库不提供自动同步脚本。升级时对照上游行为，再运行 `runtime/agentkit/tests`。

`dataagent.core.__version__` 和图元数据中的 `dataagent_versions` 都是 `0.1.0.dev0`，不读取安装元数据。PyPI 包版本写在 `pyproject.toml`，为 `0.1.0`。

## 代码分层

| 路径 | 职责 |
| --- | --- |
| `dataagent/core/` | Deep Agents 图、文件系统、摘要、Skill 和子代理 |
| `dataagent/bootstrap/`、`settings.py`、`declarations.py` | 配置、路径和扩展声明 |
| `dataagent/extensions/`、`agent.py`、`prompts/` | 把声明编译进图，并加上限额、会话目录和 Trace |
| `dataagent/semantic_client/` | 语义服务 HTTP 客户端 |
| `restapi/` | 本机 HTTP 服务 |

宿主策略放在 `build_native_middleware` 和提示中，执行循环使用 `create_deep_agent`。

## 修改 core 之后

1. 若模型可见文案跟随上游变化，整段替换，并保留 MIT 署名。
2. 运行 `pytest runtime/agentkit/tests -m 'not live'`。优先查看 Hook 顺序、编译契约、文件系统和子代理测试。
3. 摘要阈值、卸载路径或 Checkpoint 状态键有变化时，同步更新 [上下文](../architecture/context-management.md) 和 [发布](release.md)。
4. `import dataagent` 保持轻量：导入 core 时不访问网络，也不创建 Home。

`builtin-plugins/attribution-analysis` 只有说明，没有 `.plugin.json`，不能作为插件启用。它不属于 core。
