# 内置 core

`dataagent/core` 随仓库发布，安装时不再下载另一份 Deep Agents 包。产品通过 `dataagent.core.graph.create_deep_agent` 装配图。

模型能看到的工具说明和子代理说明来自 [deepagents](https://github.com/langchain-ai/deepagents)，版权归 LangChain, Inc.，许可为 MIT。文件头保留这段署名。这些句子决定模型如何使用工具。修改原文会改变运行行为；跟随上游更新时，按上游原文整段替换。

子代理状态键与 deepagents 0.7.15 对齐。仓库不提供自动同步脚本。升级时对照上游行为，再运行 `tests`。

`dataagent.core.__version__` 和图元数据中的 `dataagent_versions` 都是 `0.1.0.dev0`，不读取安装元数据。Python 发行版本独立定义在 [pyproject.toml](../../pyproject.toml)。

源码边界见[架构概览](architecture/overview.md)。宿主策略放在 `build_native_middleware` 和提示中，执行循环使用 `create_deep_agent`。

## 修改 core 之后

1. 若模型可见文案跟随上游变化，整段替换，并保留 MIT 署名。
2. 按[测试指南](testing.md)验证改动。优先检查 Hook 顺序、编译契约、文件系统和子代理。
3. 摘要阈值、卸载路径或 Checkpoint 状态键有变化时，同步更新 [上下文](architecture/context-management.md) 和 [发布](release.md)。
4. 验证 `import dataagent` 的惰性导入边界，以及导入过程中不会创建 Home 或访问网络。
