# 扩展生命周期

扩展从磁盘声明变成图参数，经过定位、编译和按轮刷新。

## 编译

`compile_extensions` 按 `plugins.enabled` 的顺序处理插件：

1. 读取 `system_prompt` 并拼接
2. 编译插件 Skill，再编译 Home 的独立 Skill
3. 加载工具，名字变成 `<plugin-id>__<local-id>`
4. 收集插件 Hook
5. 读取子代理 JSON

然后编译根 Hook：插件条目在前，独立条目在后。子代理各自再编译自己的 Hook、工具子集和 Skill 子集。

Python 加载器在一次编译内按文件缓存模块。入口必须落在对应根目录内。

MCP 工具在编译前就已经是 `BaseTool`。它们先进入注册表，名字冲突则失败。

## 指纹

`extension_revision` 对下列路径做 SHA-256：配置文件、相关 `.env`、已启用插件目录、Home `skills/`、Home `.mcp.json`、各 Hook 入口文件。目录成员和纳秒级 mtime、ctime、大小参与摘要。目录符号链接成环时跳过。业务输入和会话生成物不参与。

忽略的目录名包括 `__pycache__`、`.git`、`.venv`、`node_modules`、`.pytest_cache`、`.ruff_cache`。

## 缓存与一致性

REST 的缓存键是线程 ID 加输入绑定。指纹未变且键存在时直接复用 Agent。

指纹变化且这次调用允许创建会话时：

1. `refresh_extensions`
2. 若 MCP 映射变了，重新加载工具
3. `create_agent`，带上新的 `extension_revision`
4. 若加载期间指纹又变，放弃这次结果并报错
5. 成功后清空最多 32 条缓存，换成新 Runtime

进行中的流不更换图。失败的刷新不替换 Runtime。

Skill 中间件把修订号放进私有状态。修订变化才重新扫描元数据，避免每轮重读全部 Skill 正文。

SDK 没有这层缓存。调用方比较 `extension_revision`，自行 `refresh_extensions` 后再 `create_agent`。

## 仍然要重启的变更

模型连接、限额、`server`、`semantic`、Home 和输入目录留在原来的 Runtime 上。`refresh_extensions` 不更新它们。
