# step2_run_schema_linker 使用说明

## 1. 脚本位置与用途

- 脚本路径

  `nl2sql/runner/step2_run_schema_linker.py`

- 用途

  执行 Schema Linking 流水线（Step2a ~ Step2i），完成问题与数据库 schema 的对齐，包括：

  - Step2a: 数据集预处理（加载 dev.json + SQLite schema）
  - Step2b: 关键词抽取与值检索
  - Step2c: 列语义匹配召回
  - Step2d: LLM 直接召回
  - Step2e: 值匹配召回（LSH + 枚举值描述）
  - Step2f: 值检索阈值召回
  - Step2g: SQL 反向召回
  - Step2h: Join 关系召回
  - Step2i: 格式化输出（DDL schema 文件）

---

## 2. 必要全局配置（`nl2sql/config.py`）

该 runner 不直接接收“路径类参数”，核心路径与阈值均来自 `nl2sql/config.py`。

执行前先在 `config.py` 中确认以下配置参数均正确，且对应文件/目录存在。

### 2.1 数据源相关

- `DEV_JSON`：测试数据集的 json 文件路径
- `BIRD_DB_DIR`：SQLite 数据库目录
- `FEW_SHOT_PATH`：few shot 文件路径。step1h 会输出 few-shot 文件到此路径，保持不变即可。

### 2.2 LLM / 向量模型

- `LLM_PROVIDER` / `LLM_PROVIDER_CONFIGS`：LLM 模型名称、地址和 API Key
- `EMBEDDING_MODEL` ：Step2b/2c 等使用的向量模型

### 2.3 Step2 相关路径（可选）

详见 `config.py` 中 Step2 区域，这里列出关键路径示意：

- 日志与状态：

  - `SCHEMA_LINKER_LOG_DIR`：`nl2sql/log/schema_linker/`
  - `STEP2_STATE_PATH`：`nl2sql/log/schema_linker/state.json`

- workspace 中间结果（见第 6 节）：

  - `STEP2A_DATASET_SAVE_PATH`：`workspace/schema_linker/dataset/dataset.pkl`
  - `STEP2B_KEYWORDS_SAVE_PATH`：`workspace/schema_linker/keywords/dataset.pkl`
  - `STEP2C_COLUMN_MATCH_SAVE_PATH`：`workspace/schema_linker/column_match/dataset.pkl`
  - `STEP2D_LLM_DIRECT_SAVE_PATH`：`workspace/schema_linker/llm_direct/dataset.pkl`
  - `STEP2E_COLUMN_VALUE_SAVE_PATH`：`workspace/schema_linker/column_value/dataset.pkl`
  - `STEP2F_VALUE_RETRIEVAL_SAVE_PATH`：`workspace/schema_linker/value_retrieval/dataset.pkl`
  - `STEP2G_SQL_REVERSED_SAVE_PATH`：`workspace/schema_linker/sql_reversed/dataset.pkl`
  - `STEP2H_JOIN_CLOSURE_SAVE_PATH`：`workspace/schema_linker/join_closure/dataset.pkl`
  - `STEP2I_OUTPUT_DDL_DIR`：`workspace/schema_linker/ddl_output/`

---

## 3. 执行方式（首次执行 + 中断后重跑）

> **建议做法**：直接全链路跑 Step2a~Step2i，若中途因故中断，再用完全相同的参数 + `--resume` 重跑即可，内部会基于 `dataset.pkl` 做断点续传。

在 `nl2sql` 上层目录运行：

注意：

- 命令执行时终端会话窗口需常驻，或者使用 `nohup` 常驻进程运行。
- 如果因机器断电或重启、会话窗口断开等原因导致程序中止，执行重跑命令。

### 3.1 首次全量执行

```bash
python -m nl2sql.runner.step2_run_schema_linker --verbose
```

### 3.2 中断后的重跑

若第一次执行中途因为断电 / 进程 kill / 终端关闭等原因中断，只需使用**完全相同的参数**，额外加上 `--resume` 即可续跑：

```bash
python -m nl2sql.runner.step2_run_schema_linker --resume --verbose
```

断点续传行为：

- Step2b ~ Step2h 会从已有的 `dataset.pkl` 与错误题目文件中恢复：
  - 已完成的题目会被跳过；
  - 仅处理剩余 pending / 失败题目。
- Step2a / Step2i 会在需要时根据现有产物重新执行，内部有轻量校验与重跑逻辑。

---

## 4. 断点续跑与错误重试机制

### 4.1 checkpoint 与保存频率

- 各子步骤在处理 pending items 时，会周期性地将当前进度写回各自的 `dataset.pkl`，并同步到 `state.json` 中。
- 频率由 `config.SCHEMA_LINKING_SAVE_INTERVAL` 控制（默认 20）：
  - 表示每处理多少个 pending question 保存一次 checkpoint。

### 4.2 错误题目记录与单步校验

- 各子步骤在内部会将失败的题目记录到 `log/schema_linker/{step2X}_error_questions.json` 中（仅存在失败题目时才生成）。
- runner 在每个步骤完成后还会做轻量“验证”（例如 Step2a 校验条数一致、Step2b~2h 校验 error_questions 是否为空），并在日志中输出结论：
  - 验证失败时不会无限重试，只做有限一次内部重跑（在子步骤脚本内部），之后错误题目会出现在对应的 error 文件中。
  - 下次带 `--resume` 重跑时，只会针对剩余失败题目和未完成题目进行处理。

---

## 5. 状态仪表盘 `state.json`

### 5.1 位置与用途

- **路径**：`nl2sql/log/schema_linker/state.json`
- **用途**：仅用于观测与调试，不参与实际业务决策或断点续传逻辑。
  - 断点续传完全基于各步骤的 `dataset.pkl` 与错误题目文件。
  - `state.json` 主要用于查看每个 Step2 子步骤的运行历史、当前进度和中断点。

### 5.2 结构概览（简化）

```json
{
  "version": 1,
  "created_at": "...",
  "last_update_time": "...",
  "steps": {
    "2b": {
      "step_id": "2b",
      "name": "keywords",
      "status": "running|success|failed",
      "completed_questions": 3,
      "total_questions": 3,
      "start_time": "...",
      "last_update_time": "...",
      "last_run_index": 2,
      "last_error": null,
      "history": [
        {
          "run_index": 1,
          "kind": "baseline|resume",
          "status": "running|success|failed",
          "execution_start_time": "...",
          "execution_end_time": null,
          "duration_seconds": null,
          "completed_before_run": 0,
          "completed_after_run": 1,
          "delta_completed": 1,
          "args": {...},
          "error": null
        }
      ]
    }
  }
}
```

说明：

- `steps[step_id]` 中的 `completed_questions` / `total_questions` 是**当前**累计进度（严格在 `dataset.pkl` 写盘之后更新，保证一致）。
- `history` 是**追加式**运行历史，记录 baseline / resume 等多轮运行：
  - 每次通过 Step2 runner 启动某个 step，都会 append 一个新的 run。
  - `kind` 由参数中的 `--resume` 推断（baseline / resume）。
  - `completed_before_run` / `completed_after_run` / `delta_completed` 帮助你快速看出这一轮实际做了多少工作。

### 5.3 中断与恢复在 history 中的体现

- 若进程被 kill / 崩溃：
  - 对应 run 的记录会保持：

    - `status = "running"`
    - `execution_end_time = null`
    - `duration_seconds = null`

  - 此时的 `completed_after_run` 即为“中断前已经写入 `dataset.pkl` 的题目数”。
- 下次用 `--resume` 重跑：
  - 会追加一个新的 run（`kind = "resume"`，`status` 最终为 `success` 或 `failed`）。
  - `completed_before_run` = 上一次 run 后的题目数，`completed_after_run` = 本轮最终题目数。

---

## 6. 输出目录结构与产物

### 6.1 workspace 中间结果（`workspace/schema_linker/`）

```text
nl2sql/workspace/schema_linker/
  dataset/          -- Step2a 输出
    dataset.pkl
  keywords/         -- Step2b 输出
    dataset.pkl
  column_match/     -- Step2c 输出
    dataset.pkl
  llm_direct/       -- Step2d 输出
    dataset.pkl
  column_value/     -- Step2e 输出
    dataset.pkl
  value_retrieval/  -- Step2f 输出
    dataset.pkl
  sql_reversed/     -- Step2g 输出
    dataset.pkl
  join_closure/     -- Step2h 输出
    dataset.pkl
  output/           -- Step2i 汇总输出（中间形式）
    dataset.pkl
  ddl_output/       -- Step2i 最终 DDL schema 文件
    recall_first_schema.json
    precision_first_schema.json
    fullschema.json
```

### 6.2 日志与错误题目（`log/schema_linker/`）

```text
nl2sql/log/schema_linker/
  run_schema_linker.log                -- Step2 runner 总览日志

  step2a_load_dataset.log              -- 各子步骤主日志（示例）
  step2b_keywords_and_retrieval.log
  step2c_column_match_linker.log
  step2d_llm_direct_linker.log
  step2e_value_match_linker.log
  step2f_value_retrieval_linker.log
  step2g_sql_reversed_linker.log
  step2h_join_closure_linker.log

  step2a_error_databases.json          -- Step2a：按库级记录失败情况（仅有错误时存在）
  step2b_error_questions.json          -- Step2b：失败题目列表
  step2c_error_questions.json          -- Step2c：失败题目列表
  step2d_error_questions.json          -- Step2d：失败题目列表
  step2e_error_questions.json          -- Step2e：失败题目列表
  step2f_error_questions.json          -- Step2f：失败题目列表
  step2g_error_questions.json          -- Step2g：失败题目列表
  step2h_error_questions.json          -- Step2h：失败题目列表

  state.json                           -- Step2 状态仪表盘（见第 5 节）
```

---

## 7. 参数配置说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--only-step` | str | None | 只执行单个 Step2 子步骤（可用：`2a`~`2i`） |
| `--step-range` | str | None | 执行步骤范围，格式 `2a-2f` 或 `2a,2f`（包含终点） |
| `--resume`, `-r` | flag | false | 断点续传，仅对 Step2b~2h 生效，基于各步骤的 `dataset.pkl` 和 `error_questions` |
| `--limit`, `-l` | int | None | 只处理前 N 个问题（全链路 / 当前选中步骤） |
| `--question-range` | str | None | 只处理 Step2b~2h pending_items 中 question_id ∈ [start,end)，例 `--question-range 0,100` |
| `--question-ids` | str | None | 只处理指定 question_id（逗号分隔），例 `--question-ids 89,90,91`；对 Step2a~2h 生效 |
| `--verbose`, `-v` | flag | false | 启用详细日志输出（子进程直接输出到控制台） |
| `--list-steps` | flag | false | 列出所有可用 Step2 子步骤及对应脚本是否存在 |
| `--force-rerun-state` | flag | false | 仅重置 `log/schema_linker/state.json`，不影响任何中间产物；通常只在调试状态仪表盘时使用 |

> 额外：环境变量 `SCHEMA_LINKING_SAVE_INTERVAL` 控制 checkpoint 保存间隔（默认 20），可在测试时调小以更细粒度地观察中断点与恢复行为。

---

## 8. 最终产物

当 Step2i 执行完成后，最终 schema 文件默认输出到：

- `nl2sql/workspace/schema_linker/ddl_output/recall_first_schema.json`
- `nl2sql/workspace/schema_linker/ddl_output/precision_first_schema.json`
- `nl2sql/workspace/schema_linker/ddl_output/fullschema.json`
