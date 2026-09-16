# BIRD 评测操作手册

统一入口为 `dataagent-bird-benchmark`，也可用 `python -m dataagent.core.suite.builtin_suites.bird_benchmark.run_bird`。每次运行配置一个评测模型；比较多个模型时分别指定模型和运行目录。

## 1. 安装与预处理

在已具备项目 `nl2sql` 或 `all` 依赖的 Python 环境安装 wheel：

```sh
python -m pip install /path/to/datagallery_dataagent-0.2.0-py3-none-any.whl
dataagent-bird-benchmark --help
```

准备 `dev.json`、`dev_tables.json`、`dev_databases/<db_id>/<db_id>.sqlite`、对应 `database_description/`，以及生成 OSI 所用的 `train_cache.json`。这些数据及密钥均不随 wheel 分发。

`prepare` 生成或补全列描述缓存，生成 OSI YAML，导入 semantic-service 并验证导入结果。预处理的模型、网关、凭据和 HTTP 参数独立配置；更换评测模型不会自动重建描述。以下示例只处理一个数据库，去掉 `--db-id` 可准备全部 BIRD 数据库：

```sh
dataagent-bird-benchmark prepare \
  --bird-data-dir /data/bird/dev \
  --train-cache /data/bird/few_shot_data/train_cache.json \
  --db-id california_schools \
  --preprocess-root /runs/bird-assets \
  --preprocess-model preprocessing-model \
  --preprocess-api-base https://preprocess.example.invalid/v1 \
  --preprocess-api-key-file /secure/preprocess.key \
  --semantic-service-url http://semantic-service:32000 \
  --semantic-db-prefix bird
```

`--preprocess-model`、`--preprocess-api-base`、数据目录、train cache、预处理目录及服务地址是 prepare 的必需配置，可由对应环境变量提供。使用 `--preprocess-api-key-file` 指定独立凭据；未指定文件时使用 `LLM_API_KEY`／`DEEPSEEK_API_KEY` 环境变量。密钥文件支持单个 key 或含这些变量的 dotenv 文件。

使用 `verify` 检查预处理产物，使用 `import` 将产物导入 semantic-service：

```sh
dataagent-bird-benchmark verify \
  --preprocess-root /runs/bird-assets \
  --db-id california_schools --semantic-db-prefix bird

dataagent-bird-benchmark import \
  --preprocess-root /runs/bird-assets \
  --db-id california_schools --semantic-db-prefix bird \
  --semantic-service-url http://semantic-service:32000
```

评测的 `--preprocess` 有三个值：默认 `skip` 使用已准备好的语义服务；`reuse` 校验指定本地目录后评测；`prepare` 先生成并导入所选数据库，再评测。

预处理输出包括 `descriptions/`、`osi/`、`import_responses/` 和 `logs/`。全局 train 示例在 `california_schools` 的 YAML 中导入一次；只准备其他数据库时，应确认这些全局示例已经导入。值采样参数见第 3 节。

列描述请求默认不启用 TLS 证书验证。需要证书配置时使用 `DATAAGENT_OUTBOUND_SSL_SERVICES`、`DATAAGENT_OUTBOUND_MODE`、`DATAAGENT_OUTBOUND_CA_FILE`。

## 2. 如何评测

所需语义数据已导入时，运行一题：

```sh
dataagent-bird-benchmark run \
  --bird-data-dir /data/bird/dev \
  --db-id california_schools --question-id 0 \
  --semantic-service-url http://semantic-service:32000 \
  --semantic-db-prefix bird \
  --model evaluation-model \
  --api-base https://eval.example.invalid/v1 \
  --api-key-file /secure/eval.key \
  --run-dir /runs/bird-one
```

去掉选题条件即运行输入题集的全部题目。`--db-id`、`--question-id` 可重复；`--question-no` 按筛选后的列表使用从 1 开始的序号。`--exclude-known-questions` 启用包内 `resources/question_id.md` 排除表，默认关闭；`--exclude-question-ids /path/to/ids.md` 使用自定义排除表。排除发生在首轮选题，补跑使用同一题集。

受限网关可选 limited。以下示例取消生成 token 和整题时限，并将 `enable_thinking` 设置为 `false`：

```sh
dataagent-bird-benchmark run \
  --mode limited --bird-data-dir /data/bird/dev \
  --semantic-service-url http://semantic-service:32000 \
  --model evaluation-model --api-base https://eval.example.invalid/v1 \
  --api-key-file /secure/eval.key \
  --generator-max-tokens none --case-timeout none \
  --thinking omit --enable-thinking false \
  --run-dir /runs/bird-limited
```

在运行命令末尾加 `--dry-run`，可预览设置、选题数量和 worker 配额。使用 `check` 检查模型网关连接：

```sh
dataagent-bird-benchmark check \
  --model evaluation-model --api-base https://eval.example.invalid/v1 \
  --api-key-file /secure/eval.key
```

恢复同一运行目录时加 `--resume`，保持选题条件和 worker 分配不变；改变题集或 worker 布局时使用新目录。

## 3. 模式与常用参数

新运行的优先级为 CLI > 环境变量 > 模式默认值。恢复时已保存设置作为默认值继续生效，可被本次 CLI 和环境覆盖。常用环境变量包括 `BIRD_MODE`、`BIRD_WORKERS`、`BIRD_MODEL`、`BIRD_API_BASE`、`BIRD_LLM_MAX_CONCURRENCY`、`BIRD_GENERATOR_MAX_TOKENS`、`BIRD_CASE_TIMEOUT`；请求字段同样使用 `BIRD_` 前缀，预处理字段使用 `BIRD_PREPROCESS_` 前缀。

| 默认配置 | standard | limited |
| --- | --- | --- |
| worker 进程数 | 4 | 1 |
| 本次评测 LLM 调用总上限 | 无额外限制 | 1 |
| Generator token 上限 | 不发送 | 4096 |
| 整题 deadline | 不设置 | 1800 秒 |
| HTTP timeout / retries | 900 秒 / 2 次重试 | 900 秒 / 2 次重试 |
| thinking / enable_thinking / reasoning_effort | 均省略 | 均省略 |

每个 worker 串行处理题目。有限总额度 C 要求 worker 数 W ≤ C，按 `C // W` 分配，余数给前几个 worker；例如 4 个 worker、总额度 6，配额为 `[2, 2, 1, 1]`。选题少于 worker 数时只启动必要进程。闲置进程的额度不动态借给其他进程。

根据网关可用额度设置本次评测的并发上限，并为其他实验及 semantic-service 调用预留额度。

| 参数 | 含义 |
| --- | --- |
| `--workers N` | worker 数，必须为正整数 |
| `--llm-max-concurrency N\|none` | 本次评测总额度；`none` 取消限制 |
| `--generator-max-tokens N\|none` | Generator 的 token 上限；`none` 不发送此限制 |
| `--case-timeout N\|none` | 一整题 Agent chat 时限；`none` 取消整题时限 |
| `--llm-timeout N`、`--llm-num-retries N` | HTTP 超时须为有限正数；重试次数须为非负整数 |
| `--model`、`--api-base` | 模型名及独立网关；支持网关根地址、`/v1` 或完整 `/chat/completions` 地址 |
| `--api-key-file` | 从文件读取评测凭据，也可使用 `DEEPSEEK_API_KEY` 或 `LLM_API_KEY` 环境变量 |
| `--thinking enabled\|disabled\|omit` | 设置结构化 `thinking.type`，或彻底省略字段 |
| `--enable-thinking true\|false\|omit` | 设置布尔字段；裸参数等价于 `true`，`false` 会发送 JSON false |
| `--reasoning-effort VALUE` | 指定推理强度，取值按模型 API 的要求填写；`omit` 省略 |
| `--extra-body '{"temperature":0.2}'` | 额外 JSON 对象；与模型、消息、stream、token、思考或传输控制冲突的键会报错，不能放凭据 |
| `--preprocess-model`、`--preprocess-api-base`、`--preprocess-api-key-file` | 独立预处理模型、网关及凭据 |
| `--preprocess-thinking`、`--preprocess-enable-thinking`、`--preprocess-reasoning-effort`、`--preprocess-extra-body` | 与评测相同的字段省略和显式取值语义 |
| `--preprocess-llm-timeout`、`--preprocess-llm-num-retries` | 预处理 HTTP 设置，默认 900 秒 / 2 次重试 |
| `--value-mode` | `sample`、`text_distinct`、`all_distinct`；默认 `text_distinct` |
| `--text-distinct-max-cardinality` | 默认 1000 |
| `--max-values-per-column`、`--max-values-per-db`、`--max-yaml-bytes` | 默认 10000、100000、9000000 |

省略 CLI 参数时按上述优先级取值；显式 `none` 取消相应限制。思考字段使用 `omit` 表示省略，`thinking` 和 `enable_thinking` 可独立设置。

## 4. 补跑与结果查看

默认对 `deferred`（整题超时）和 `agent_error`（执行错误）补跑一次；只有 SQL 结果不匹配的题默认不补跑。`--no-retry` 关闭自动补跑；`--retry-on deferred,agent_error,incorrect` 显式将结果不匹配也纳入。每题最多增加一次补跑，不因仍然失败循环重试。

补跑默认 `--retry-case-timeout none`、`--retry-generator-max-tokens none`，包括 limited 模式；HTTP timeout/retries 及模型请求设置继承首轮。取消整题时限不代表取消 HTTP 超时。

已有运行可单独补跑或重新汇总：

```sh
dataagent-bird-benchmark retry \
  --run-dir /runs/bird-one --api-key-file /secure/eval.key

dataagent-bird-benchmark aggregate --run-dir /runs/bird-one
```

补跑中断后用 `retry --run-dir /runs/bird-one --api-key-file /secure/eval.key --resume` 继续。补跑使用该目录保存的数据路径、选题和设置。

统一运行目录的主要产物：

```text
resolved_config.json          本次解析后的非敏感设置
selected_questions.json       实际选题
summary_initial.json          首轮统计
summary.json                  完成运行后的有效统计
effective_summary.json        首轮与补跑合并后的统计
full/
  workers.json                题目分片和 worker 配额
  shard_0/ ... shard_N/        首轮逐题结果
retry_once/
  plan.json                   补跑选题和限制
  retry_summary.json          仅补跑统计
  shard_0/ ... shard_N/        补跑逐题结果
```

每个 shard 包含 `manifest.json`、逐题更新的 `summary.json`、deferred 清单；每题目录为 `<db_id>__q<question_id>/`，含 `agent_config.yaml`、`question.json`、`stats.json`、预测及 gold SQL、`final_state.json`、`messages.json`、日志和 `workspace/.memory/context_dump/`。异常时另有 `exception.txt`。manifest、summary 和 stats 记录实际请求控制、生成预算和 worker 额度。

SQL 使用只读 SQLite 执行，通过返回行集合比较判分，忽略顺序及重复行。统计包括耗时和 Agent 返回的 `llm_total_tokens`。`accuracy` 以全部已选题为分母，超时和执行错误计为答错。

结果按 `(db_id, question_id)` 唯一键合并，重复题报错。补跑结果替换对应首轮结果，即使补跑仍然失败也采用补跑结果；不增加分母、不择优取分。发生补跑合并时，有效结果通过 `effective_attempt` 区分首轮与补跑，原始产物保留。
