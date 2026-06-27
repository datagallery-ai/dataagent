# step3b_run_nl2sql_single_combination 使用说明

## 1. 脚本位置与用途

- 脚本路径

  `nl2sql/runner/step3b_run_nl2sql_single_combination.py`

- 用途

  执行单组合 NL2SQL 全流程（Generation → Selection），包含三路并行 SQL 生成、执行校验、TOP 1 SQL选择和置信度计算。

  只需运行一次，采用 recall_first_schema.json。

## 2. 必要全局配置（`nl2sql/config.py`）

执行前先在 config.py 中确认以下配置参数均正确，且对应文件存在。

### 2.1 数据源相关

- `DEV_JSON`：dev 集 json 路径
- `BIRD_DB_DIR`：SQLite 数据库目录
- `FEW_SHOT_PATH`：few shot 文件路径

### 2.2 LLM

- 不用`LLM_PROVIDER` / `LLM_PROVIDER_CONFIGS`里的默认LLM，使用运行时参数传入的 or_glm51，即 OpenRouter 提供的 GLM 5.1

### 2.3 Schema 文件

- `STEP2I_OUTPUT_DDL_DIR`：schema 文件目录（默认为 `workspace/schema_linker/ddl_output/`）
- 要用的 schema 文件：
  - `recall_first_schema.json`

## 3. 执行方式

在 `nl2sql` 上层目录执行以下命令：

注意：

- 命令执行时终端会话窗口需常驻，或者使用 `nohup` 常驻进程运行。

- 如果因机器断电或重启、会话窗口断开等原因导致程序中止，重新执行相同命令即可（自动断点续跑）。
- 不要改变每个组合运行时除了run-id以外的参数，max-workers可视llm提供商情况适度调低

### 3.1 首次全量执行

```bash
python -m nl2sql.runner.step3b_run_nl2sql_single_combination \
    --run-id recall-first_round1 \
    --schema-json recall_first_schema.json \
    --llm-provider or_glm51 \
    --fr-force-on \
    --max-workers 5
```

或者 nohup 常驻进程运行：

```bash
nohup python -m nl2sql.runner.step3b_run_nl2sql_single_combination \
    --run-id recall-first_round1 \
    --schema-json recall_first_schema.json \
    --llm-provider or_glm51 \
    --fr-force-on \
    --max-workers 5 > output.log 2>&1 &
```

### 3.2 中断后的重跑

若第一次执行中途因为断电 / 进程 kill / 终端关闭等原因中断，只需使用**完全相同的命令**即可续跑（无需额外参数）：

```bash
python -m nl2sql.runner.step3b_run_nl2sql_single_combination \
    --run-id recall-first_round1 \
    --schema-json recall_first_schema.json \
    --llm-provider or_glm51 \
    --fr-force-on \
    --max-workers 5
```

断点续传行为：

- 脚本启动时自动扫描 `workspace/sql_generation/{run_id}/selector/` 目录；
- 已存在 `q_{qid:04d}_selected.json` 的题目自动跳过；
- 仅处理剩余未完成题目。

> **重要备注**
>
> 1. **每轮/checkpoint 用 run-id 后缀区分**：不同轮次请修改 `_round1` 后缀，例如
>    `recall-first_round1`、`recall-first_round2`，确保输出目录不会互相覆盖。且后续 step4a 会用这一步的 run_id 作为 `--source-run-id`
>
> 2. 一定使用 `--llm-provider` 传入的 or_glm51
>
> 3. step3b启动后，检查log的配置是否和运行参数一样，例如：
>
>    ```json
>    2026-05-23 21:10:20 [INFO] step3b - ============================================================
>    2026-05-23 21:10:20 [INFO] step3b -   Step3b: 单组合全流程 Runner
>    2026-05-23 21:10:20 [INFO] step3b -   Time         : 2026-05-23 13:10:20
>    2026-05-23 21:10:20 [INFO] step3b -   Run ID       : recall-first_round1
>    2026-05-23 21:10:20 [INFO] step3b -   Dev JSON     : nl2sql/data/dev/dev.json
>    2026-05-23 21:10:20 [INFO] step3b -   DB Dir       : {HOME}/nl2sql/metavisor/nl2sql/data/dev/dev_databases
>    2026-05-23 21:10:20 [INFO] step3b -   Few-Shot     : nl2sql/workspace/few_shot/few_shot_examples.json
>    2026-05-23 21:10:20 [INFO] step3b -   Schema       : recall_first_schema.json
>    2026-05-23 21:10:20 [INFO] step3b -   LLM Provider : or_glm51
>    2026-05-23 21:10:20 [INFO] step3b -   FR Force On  : True
>    2026-05-23 21:10:20 [INFO] step3b -   Max Workers  : 5
>    2026-05-23 21:10:20 [INFO] step3b -   Max Retries  : 5
>    2026-05-23 21:10:20 [INFO] step3b -   CoT Output   : ON
>    2026-05-23 21:10:20 [INFO] step3b -   Log File     : {home}/mvuser/nl2sql/metavisor/nl2sql/log/step3b/recall-first_round1/step3b_20260523_131020.log
>    2026-05-23 21:10:20 [INFO] step3b - ============================================================
>    ```
>
>    

## 4. 错误重试机制

### 4.1 断点续跑

- 脚本启动时自动扫描 `workspace/sql_generation/{run_id}/selector/` 目录
- 已存在 `q_{qid:04d}_selected.json` 的题目自动跳过
- 中断后重新执行相同命令即可续跑

### 4.2 全局重试

- 参数：`--max-retries N`（默认 5）
- 首轮执行中遇到的 LLM 错误会记录到 errors.json
- 首轮结束后，自动从 errors.json 中读取失败题目进行重试
- 重试成功后自动从 errors 中移除

> **如果全局重试多次后（达到max-retries）仍有失败题目**：调高 `config.py` 中 `LLM_PARSE_FAIL_MAX_RETRIES`（默认 10），
> 依次尝试 15 → 20 → 25 → 30 逐步叠加，然后重跑相同命令即可（断点续传只处理剩余失败题目）。

### 4.3 错误类型

- `LLMParseMaxRetriesExceeded`：LLM 响应解析重试耗尽 → 整题跳过
- `LLMMaxRetriesExceeded`：LLM 网络调用重试耗尽 → 整题跳过
- 其他未预期异常 → 整题跳过

## 5. 输出目录结构

```
nl2sql/workspace/sql_generation/{run_id}/
  generation/                          -- SQL 生成中间文件
    q_{qid:04d}.json
  selector/                            -- Selector 选择结果（最终产物），包含 top1 sql 和置信度 confidence
    q_{qid:04d}_selected.json
  cot/                                 -- CoT 思维链记录（仅当 config.COT_OUTPUT_ENABLED=True 且单题完全成功时写入）
    q_{qid:04d}_cot.json

nl2sql/log/step3b/{run_id}/
  errors.json                          -- 错误记录
  step3b_{timestamp}.log               -- 运行日志
```

## 6. 参数配置说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--run-id` | str | `config.STEP3B_RUN_ID` (`default_run`) | 运行 ID，决定输出子目录名 |
| `--schema-json` | str | `config.STEP3B_SCHEMA_JSON` (`recall_first_schema.json`) | DDL schema 文件名 |
| `--llm-provider` | str | `config.STEP3B_LLM_PROVIDER` 或 `config.LLM_PROVIDER` | LLM 供应商标识，此处使用 `or_glm51` |
| `--fr-force-on` | flag | `config.STEP3B_FR_FORCE_ON` (false) | FR 强制模式（强制非 single 题执行 full_review） |
| `--max-workers` | int | `config.STEP3B_MAX_WORKERS` (5) | 题目级并行度 |
| `--max-retries` | int | `config.STEP3B_MAX_GLOBAL_RETRIES` (5) | 全局重试轮数 |
| `--force-rerun` | flag | false | 全量重跑：忽略已存在的 selector 输出，对所有题重新跑 generation+selection；与 `--rerun-qids` 互斥（后者优先） |
| `--rerun-qids` | str | None | 单题重跑：仅重跑指定题号（逗号/空格/分号分隔），例 `--rerun-qids "42,108,256"`；启用后忽略 completed 状态及 `--force-rerun` |
| `--verbose` | flag | false | 启用 DEBUG 级别日志 |
| `--dev-json-file` | str | `config.DEV_JSON` | 题目文件路径（首选参数）；常见场景：dev_diff.json 差异重跑 / train 训练集 |
| `--few-shot-file` | str | `config.FEW_SHOT_PATH` | Few-shot 示例文件路径（覆盖 ICLGenerator 运行时读取的 `config.FEW_SHOT_PATH`） |
| `--bird-db-dir` | str | `config.BIRD_DB_DIR` | BIRD SQLite 数据库目录（覆盖 `config.BIRD_DB_DIR`） |
| `--bird-data-dir` | str | `config.BIRD_DATA_DIR` | BIRD 数据集根目录，同时派生 DEV_JSON / BIRD_DB_DIR / BIRD_TABLES_JSON；优先级低于各独立参数 |
| `--dev-json` | str | None | ⚠️ 旧参数，兼容保留，建议改用 `--dev-json-file` |

## 7. 产物

每组运行完成后，最终产物为 `workspace/sql_generation/{run_id}/selector/` 下的 JSON 文件。

每个文件包含字段：
- `question_id`：题号
- `db_id`：数据库 ID
- `sql_candidates_after_revision`：经过校验修正后的 SQL 候选列表
- `confidence`：等权多数投票置信度
- `top1_sql`：等权 top1 SQL
- `full_review_sql`：Full Review 选出的 SQL（仅非 single 题有此字段）
