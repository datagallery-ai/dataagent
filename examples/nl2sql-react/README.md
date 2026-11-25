# 运行 nl2sql-react ver6.4

插件默认不启用。将本目录 [config.example.yaml](config.example.yaml) 的 `plugins` 配置
合入实际启动使用的 config.yaml，并填写 SQLite 绝对路径即可启用；其他启动参数不变。
功能、预算、结果选择及配置边界见 [插件说明](../../dataagent/plugins/nl2sql-react/README.md)。

## SDK 示例

在仓库根目录的 Python 3.12 环境中：

1. 将 `.env.example` 复制为本目录 `.env`，填写模型配置与数据库路径。
2. 在本目录创建 question.json，例如：

   ```json
   {"question": "How many schools are there?", "evidence": ""}
   ```

3. 执行 `python examples/nl2sql-react/run.py`。

`run.py` 读取本目录的 `config.example.yaml` 和 `.env`，由宿主按 `dataagent.model`
加载模型。供应商的请求参数可填在 `models.<别名>.extra_body`。
从旧版升级时，将 `plugins.enabled` 和 `plugins.config` 下的 `nl2sql-local` 改为 `nl2sql-react`，
并移除旧配置中的 `preserve_reasoning_content`。
如果供应商要求跨工具调用回传 `reasoning_content`，需要选用支持该字段的模型客户端。

每次答案执行保存 query.sql、data.json、data.csv、metadata.json，交付选择保存 final.json。
根 Agent 可继续组织回答。多条成功 SQL 通过 select_result 选择；重复任务复用结果。
SDK 可从最终消息的 `additional_kwargs` 读取本轮的 Local 结果和所选 `query_id`。
