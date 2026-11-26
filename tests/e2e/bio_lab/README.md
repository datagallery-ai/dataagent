# Bio Lab 一键评测

评测入口通过原有 SDK 与插件加载机制使用 `nl2sql` 子 Agent 和 `data-qa` 插件，不新增工具注册机制。
评测启用 `data-qa` 后，框架会把插件的 `prompts/routing.md` 加入主 Agent 的系统提示词。评测入口只提交用户问题并处理反馈。

评测生成的 `config.yaml` 包含以下插件配置（完整配置还包含模型和 NL2SQL 数据库参数）：

```yaml
plugins:
  enabled:
  - nl2sql
  - data-qa
  config:
    data-qa:
      database: bio_lab
```

工具名称为 `data-qa__get_ontology_description_tool` 和 `data-qa__request_human_feedback`，由 `.plugin.yaml` 声明并通过框架自动加前缀。数据库名来自插件配置，不暴露为模型参数。
反馈工具通过 `interrupt` 暂停，宿主须提供 checkpointer 并用同一 thread_id 恢复；评测已处理。
题目、反馈队列和 system prompt 注入方式保持不变；证据判分已修正：只读 TC 仍回放 SQL，TC-W 使用查询执行时的工具结果，避免后续合法写入改变历史查询的行数。
TC20 按 schema 证据核验：要求实际成功的 ontology 调用及非空表列信息，再检查原有答案规则，不强制执行 SQL；反馈报告记录带 `data-qa__` 前缀的实际调用。

## 运行

从仓库根目录执行：

```bash
uv sync --locked
uv run python tests/e2e/bio_lab/run.py \
  --env-file /absolute/path/model.env --output /tmp/bio-direct-run-01
```

环境文件包含 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY`；不要提交凭据。
默认运行 TC 22 题 + TC-W 15 题，使用随包 mock schema 和真实模型。
输出目录必须尚不存在。两个题组分别写入 `TC/` 和 `TC-W/`。
选择题组/题号可加 `--group TC-W --case 1 11`，超时可加 `--timeout 600`。
`--group quick` 为快速只读题；`--group bad_cases` 保留旧题库入口。

先只准备全部题目、隔离数据库和配置，不请求模型：

```bash
uv run python tests/e2e/bio_lab/run.py \
  --prepare-only --output /tmp/bio-direct-prepared-01
```

默认不访问真实语义服务；明确需要时指定 `--semantic-url`。
每题有独立 SQLite 副本，每个题组有独立 `DATAAGENT_HOME`。
TC-W 的三个业务 skill 脚本只操作该题 `USER_SQLITE_PATH`，确认回答来自原题库，
通过真实 LangGraph interrupt/resume 消费，不自动批准额外操作。
脚本 shell 操作运行在宿主机；请在适合运行这些评测脚本的环境执行。

## 文件与判分

- `fixtures/`：旧题库、schema 响应、SQLite 数据库、SQL 规则、3 个业务 skill；运行不依赖旧 runtime。
- 问数流程和工具实现分别位于 `dataagent/plugins/data-qa/prompts/` 和 `dataagent/plugins/data-qa/tools/`。
- `execution.py`：确认恢复、shell 证据（含 skill 目录下的 `cd && python`）。
- `write_assertions.py`：TC-W 数据库副作用断言。
- `<output>/<group>/results.json`：结果、SQL/shell 证据、确认记录及耗时。
- 每题 session 的 traces：保留框架本地 tracing；不承诺旧版 context_dump 格式。

不把“未改数据库”直接视为成功；需要符合原业务拒绝/创建断言和实际工具证据。
保留 `requires_manual_review` 和非阻断断言状态。

离线回归（无真实模型/语义服务）：

```bash
cd .
uv run pytest tests/test_bio_lab*.py -q
```
