# 扩展生命周期

扩展从磁盘声明变成图参数，经过定位、编译和按轮刷新。

## 编译

`prepare_runtime` 定位扩展、解析插件依赖并绑定声明来源。`compile_extensions` 在装配 Agent 时按依赖优先顺序接收已选插件：

1. 读取 `system_prompt_file` 并拼接
2. 编译插件 Skill，再编译 Home 的独立 Skill
3. 加载工具，名字变成 `<plugin-id>__<local-id>`
4. 收集插件 Hook
5. 读取子代理 YAML

然后编译根 Hook：插件条目在前，独立条目在后。子代理各自再编译自己的 Hook、工具子集和 Skill 子集。

Python 加载器在一次编译内按文件缓存模块。入口必须落在对应根目录内。

宿主先发现 MCP 工具，并以 `BaseTool` 传入编译器。编译器先放入 MCP 和 A2A 工具，再注册本地插件工具。重复注册名会导致装配失败。对外声明与注册名见[扩展声明](../../zh/reference/extension-schema.md)。

## 指纹

`extension_revision` 对配置文件、所选环境文件、已启用插件目录、Home `skills/`、Home `.mcp.yaml` 及绑定的 Agent Hook、工作流 Hook 入口生成 SHA-256 指纹。摘要使用路径、目录成员和文件元数据（纳秒级 mtime、ctime 和大小）。遍历会跳过成环的目录符号链接。业务输入和会话生成物不参与。

忽略的目录名包括 `__pycache__`、`.git`、`.venv`、`node_modules`、`.pytest_cache`、`.ruff_cache`。

<a name="refresh-and-cache"></a>

## 缓存与一致性

REST 的缓存键是线程 ID 加输入绑定。指纹未变且键存在时直接复用 Agent。

指纹变化且这次调用允许创建会话时：

1. `refresh_extensions`
2. 若 MCP 映射变了，重新加载工具
3. `create_agent`，带上新的 `extension_revision`
4. 若加载期间指纹又变，放弃这次结果并报错
5. 成功后清空旧 Agent 缓存，接受新 PreparedRuntime、MCP 工具和指纹。新 Agent 加入缓存，超过 32 项时淘汰最久未使用的条目

进行中的流不更换图。失败的刷新不替换 PreparedRuntime。

Skill 中间件把修订号放进私有状态。修订变化才重新扫描元数据，避免每轮重读全部 Skill 正文。

SDK 调用方自行决定何时重建 Agent。自建宿主可用 [`dataagent.host`](../../zh/reference/python-api.md#host-api) 比较指纹并准备候选运行时，完成工具发现和 Agent 装配后再发布。插件作者可按[插件指南](../../zh/guides/integrate/extensions/plugins.md)更新已安装的扩展。

## 仍然要重启的变更

`refresh_extensions` 重读插件、根 Hook、MCP 和 A2A 出站配置。模型连接、限额、`dataagent.entry`、`server`、`semantic`、Home 和默认输入目录沿用原 PreparedRuntime。入站 A2A Card 和鉴权由宿主启动时确定。改这些设置后重启宿主，SDK 调用方重新运行 `prepare_runtime`。
