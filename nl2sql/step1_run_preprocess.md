# Step1 Run Preprocessor README

## 1. 执行前需要确认的参数（`nl2sql/config.py`）

- `BIRD_DB_DIR`
  - 数据库路径。例如 dev 数据集的 BIRD_DB_DIR 默认是 `nl2sql/data/dev/dev_databases`。
- `BIRD_TABLES_JSON`
  - 包含所有库表列的 json。例如 dev 数据集的 BIRD_TABLES_JSON 默认是 `nl2sql/data/dev/dev_tables.json`。
- `DEV_JSON`
  - 题目集 json，仅 step1h 生成 few-shot 时使用。
- `STEP1_PREPROCESS_WORKSPACE_DIR`
  - 全部 9 个 builder 产物根目录，默认 `nl2sql/workspace/preprocess/`。
- `STEP1_PREPROCESS_LOG_DIR`
  - 日志与 state 根目录，默认 `nl2sql/log/preprocess/`。
- `FEW_SHOT_CACHE_DIR`
  - step1h 输入缓存目录，默认 `nl2sql/data/few_shot_data/`，**必须**预先包含 `train_embeddings.npy + train_cache.json`。
- `FEW_SHOT_PATH`
  - step1h 最终输出 few-shot 文件，默认 `nl2sql/workspace/few_shot/few_shot_examples.json`。
- `EMBEDDING_MODEL` / `EMBEDDING_DEVICE`
  - 默认 `BAAI/bge-large-zh-v1.5` / `cpu`，被 step1c / step1f2 / step1g 共用；如果需要使用 GPU 请改为 `cuda`。
- `LLM_PROVIDER` / `LLM_PROVIDER_CONFIGS`
  - step1b（列描述生成）与 step1f1（值枚举解析）使用，建议 deepseek-v4-flash。

## 2. 执行方式

### 2.1 首次全量执行

```bash
python -m nl2sql.runner.step1_run_preprocess --verbose
```

### 2.2 中断后的重跑

若第一次执行中途因为断电 / 进程 kill / 终端关闭等原因中断，只需使用**完全相同的命令**即可续跑（Step1 默认启用断点续传，无需额外参数）：

```bash
python -m nl2sql.runner.step1_run_preprocess --verbose
```

断点续传行为：

- 每个 builder 启动时读取自己的 state 文件（`nl2sql/log/preprocess/state/<step_name>.json`）；
- 已在 `completed_keys` 中的 db / 列 / 题目自动跳过；
- 仅处理剩余未完成项。

### 2.3 参数说明

- `--verbose` / `-v`
  - 输出更详细日志（DEBUG 级）。
- `--force-rerun`
  - 忽略 state，按当前 `BIRD_TABLES_JSON` 全量重跑指定 step / db。
- `--db-ids csv`
  - 指定数据库列表，例如 `--db-ids financial,student_club`。
- `--steps csv`
  - 指定子步骤名，例如 `--steps step1f1_extract_value_enum,step1f2_build_value_desc_vectors`；省略则按 step1a→step1h 顺序执行全部 9 个 builder。

## 3. 输出、错误、日志分别在哪

### 运行态输出

这些文件用于断点续跑：

- 运行态输出文件根目录：`nl2sql/workspace/preprocess/`
- 整轮 run 摘要：`nl2sql/workspace/preprocess/summary.json`
- 状态 log 目录（平铺）：`nl2sql/log/preprocess/state/`
- 每个子步骤独立 state 文件：`nl2sql/log/preprocess/state/<step_name>.json`，例如：
  - `step1a_build_schema_cache.json`
  - `step1b_enhance_column_desc.json`
  - `step1c_build_column_vectors.json`
  - `step1d_build_join_graph.json`
  - `step1e_build_lsh_indexes.json`
  - `step1f1_extract_value_enum.json`
  - `step1f2_build_value_desc_vectors.json`
  - `step1g_build_value_vector_db.json`
  - `step1h_build_few_shot_examples.json`

每个 state 文件采用 history 追加机制（北京时间 +08:00），`completed_keys` 跨 history 累积，顶层 `status / result / error / updated_at` 反映最近一次执行。

### 产物输出

- 表/列/采样值缓存：`nl2sql/workspace/preprocess/rest_cache/{table_list_cache, table_columns_info_cache, columns_sample_values_cache}.json`
- 列向量库：`nl2sql/workspace/preprocess/column_vector_store/{db_id}/{vectors.npy, metadata.pkl}`
- JOIN 图：`nl2sql/workspace/preprocess/join_relations/{db_id}.json`
- LSH 索引：`nl2sql/workspace/preprocess/lsh_indexes/{db_id}/lsh_index.pkl`
- 值枚举：`nl2sql/workspace/preprocess/value_desc_enum/{db_id}.json`
- 值描述向量：`nl2sql/workspace/preprocess/value_desc_vectors/{db_id}/{vectors.npy, metadata.pkl}`
- 值向量库（用于 step2 值召回）：`nl2sql/workspace/preprocess/vector_store/{db_id}/{vectors.npy, metadata.pkl}`
- few-shot 输出：`nl2sql/workspace/few_shot/few_shot_examples.json`

### 日志输出

- 总日志目录：`nl2sql/log/preprocess/`
- 主日志（每次启动一个，北京时间命名）：`nl2sql/log/preprocess/step1_run_preprocess_<YYYYMMDD_HHMMSS>.log`
- 子步骤错误：通过主日志中 ERROR/Traceback 体现；产物校验失败时主入口直接抛 `FileNotFoundError` 并写入 summary.json 的 `failed_step / missing_outputs` 字段。



# Step1 的详细介绍

## Step1 作用

`step1_run_preprocess.py` 以 BIRD 的 `BIRD_TABLES_JSON` + sqlite 数据库为输入，**在本地工作区生成下游 schema_linker / step3b 直接消费的全部 artifacts**（schema 缓存、列描述、列向量、JOIN 图、LSH 索引、值枚举、值向量库、few-shot 示例）。

按 step1a→step1h 顺序串行执行以下 9 个 builder：

| 序号 | step_name | 功能 | LLM | Embedding | 断点粒度 |
|------|-----------|------|-----|-----------|----------|
| 1 | `step1a_build_schema_cache` | 从 BIRD JSON + sqlite 拉取表/列/采样值到 rest_cache | ❌ | ❌ | db |
| 2 | `step1b_enhance_column_desc` | LLM 生成列短描述（desc_short），原地回写 columns_info_cache | ✅ | ❌ | 列 |
| 3 | `step1c_build_column_vectors` | 用 `column_name \| desc_short` 编码生成列向量库 | ❌ | ✅ | db |
| 4 | `step1d_build_join_graph` | 由 dev_tables.json + sqlite FK 构建 JOIN 图 | ❌ | ❌ | db |
| 5 | `step1e_build_lsh_indexes` | datasketch MinHashLSH 索引生成 | ❌ | ❌ | db |
| 6 | `step1f1_extract_value_enum` | 解析 value_description 与 sqlite distinct 事实核对 → 列级 enum staging | ✅ | ❌ | 列 |
| 7 | `step1f2_build_value_desc_vectors` | 聚合 step1f1 列级产物 + 编码 enum 描述向量 | ❌ | ✅ | db |
| 8 | `step1g_build_value_vector_db` | 扫 sqlite 文本列采样值 + 编码生成值向量库 | ❌ | ✅ | db |
| 9 | `step1h_build_few_shot_examples` | 基于 train_embeddings 缓存 + dev 编码，生成 few-shot 示例 | ❌ | ✅ | 题目 |


由于上述依赖是严格顺序，step1_run_preprocess 默认 9 步串行执行；需要调试时可用 `--steps` 指定子集（须自行保证依赖已完成）。

## 任务执行成功的判定标志

Step1 跑完后，按以下顺序查本地产物：

### 1. 查 summary.json

```bash
nl2sql/workspace/preprocess/summary.json
```

判定标准：

- 顶层 `status == "success"`；
- `steps[]` 包含全部 9 个 builder，每个 `verification.ok == true`、`verification.missing == []`。

若 `status == "failed"`，字段 `failed_step` 会明确指出中断 step，`missing_outputs` 列出缺失产物路径。

### 2. 查各子步骤 state

```bash
nl2sql/log/preprocess/state/
```

9 个 state 文件应全部存在，依次检查：

- `status == "success"`；
- `completed_keys` 数量等于配置的库数（dev 默认 11 个）或列/题目总数（step1b/f1/h）。

如果某个 step 状态为 `running` 或 `failed`，查 `history` 最后一条的 `error` 字段定位具体原因。

### 3. 查关键产物是否齐备

以 `superhero` 库为例：

```bash
ls nl2sql/workspace/preprocess/column_vector_store/superhero/   # vectors.npy, metadata.pkl
ls nl2sql/workspace/preprocess/vector_store/superhero/          # vectors.npy, metadata.pkl
ls nl2sql/workspace/preprocess/lsh_indexes/superhero/           # lsh_index.pkl
ls nl2sql/workspace/preprocess/join_relations/superhero.json
ls nl2sql/workspace/preprocess/value_desc_enum/superhero.json
ls nl2sql/workspace/preprocess/value_desc_vectors/superhero/    # vectors.npy, metadata.pkl
```

以及全局一份的 few-shot：

```bash
nl2sql/workspace/few_shot/few_shot_examples.json
```

### 4. 查主日志末尾

主日志最后一行应为 `=== Step1 Local Preprocess Complete ===`。

## 怎么检查是否正常

建议按下面顺序检查。

### 1. 看启动日志打印的关键配置

启动时会打印这些关键项：

- `BIRD_TABLES_JSON`
- `BIRD_DB_DIR`
- `STEP1_CACHE_DIR`
- `STEP1_VALUE_VECTOR_STORE_DIR`
- `FEW_SHOT_CACHE_DIR`
- `FEW_SHOT_PATH`

以及起始的 resume 扫描摘要：

```
--- Step1 resume scan: nl2sql/log/preprocess/state ---
  step1a_build_schema_cache: status=success completed=11
  step1b_enhance_column_desc: status=success completed=4502
  ...
```

如果这些值不对，先核对 `config.py` 和相关环境变量，不要先怀疑 builder 逻辑。

### 2. 看每个 builder 的进入/完成行

每个 builder 启动时打印 `Running <step_name>`，完成后打印：

- `→ <step_name> [DONE] processed=N, skipped=M (Xs)`——有增量执行；
- `→ <step_name> [SKIPPED] all M items already completed (Xs)`——全量跳过；
- 或 `[SKIP - already completed]` 后缀——该 step state 为 `success` 且未 `--force-rerun`。

### 3. 看状态文件中的 history

重点检查：

- `nl2sql/log/preprocess/state/<step_name>.json` 顶层 `status` 是否 `success`；
- `history[-1].duration_seconds` 与期望耗时是否匹配；
- `completed_keys` 长度是否达到全量库/列/题目。

### 4. 看错误一般在哪

常见错误位置：

- 启动日志 / 控制台
  - 配置缺失、路径错误、sqlite 打不开、embedding 模型加载失败。
- 主日志 `nl2sql/log/preprocess/step1_run_preprocess_<ts>.log`
  - LLM/Embedding 批量错误、产物校验失败。
- summary.json `failed_step` / `missing_outputs`
  - 产物验证未通过的定位点。
- state 文件 `history[-1].error`
  - 子步骤内部上报的异常。

## 常见问题快速定位

### sqlite 找不到

检查：

- `BIRD_DB_DIR` 是否正确
- 当前如果要改 `BIRD_DB_DIR`，需要修改 `config.py`，不能只改环境变量
- 目录结构是否为 `BIRD_DB_DIR/<db_id>/<db_id>.sqlite`

### step1c / step1f2 / step1g 启动后长时间无日志（看似卡住）

检查：

- 大概率是**首次加载 `BAAI/bge-large-zh-v1.5` 模型**（下载 1.3GB 或本地 mmap），用任务管理器 / `nvidia-smi` 看是否有持续 IO；
- 预热命令：`python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-large-zh-v1.5')"`；
- 加 `--verbose` 可看到 `EmbeddingEncoder initialized` 这条 log（[embedding.py#L22](file:///c:/Users/humme/Desktop/%E5%B7%A5%E4%BD%9C/metavisor_ingestion/metavisor/nl2sql/preprocess/core/embedding.py#L22)）——出现即说明已加载完成。

### step1h 报 `Few-shot cache files missing`

检查：

- `nl2sql/data/few_shot_data/` 下必须同时存在 `train_embeddings.npy + train_cache.json`（[step1h#L41-L47](file:///c:/Users/humme/Desktop/%E5%B7%A5%E4%BD%9C/metavisor_ingestion/metavisor/nl2sql/preprocess/builders/step1h_few_shot_examples.py#L41)）；
- 这两个是预计算 train embedding 缓存，**必须事先准备**，不会被任何 builder 自动生成。

### LLM 失败堆积（step1b / step1f1）

检查：

- 重新跑同一命令即可（断点续传只重试 staging 文件缺失或失败的列）；
- 仍失败可调高 `LLM_PROVIDER_CONFIGS[provider].max_retries` 或临时加大 `LLM_PARSE_FAIL_MAX_RETRIES`。