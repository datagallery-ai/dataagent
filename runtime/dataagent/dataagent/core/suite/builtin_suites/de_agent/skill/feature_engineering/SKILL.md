---
name: feature_engineering
description: |
  从零开发数仓特征表。适用于根据用户需求创建新的特征表，并生成配套的 DDL（建表）与 INSERT（插入）SQL 的场景。
  不适用于：给已有表新增列（走 add_columns skill）、纯 SQL 语法规范（见 sql_rules skill）。
---

# 特征开发

根据用户的需求进行特征开发。计划内容包含但不仅限于：
- 查询相关的元数据、表、知识、命名规范等信息
- 根据查询到的相关内容，编写口径冻结表，并调用 `freeze_calibration_to_ir` 将其融入 DataTaskIR
- 编写DDL/INSERT的SQL, 必须先写DDL，再写INSERT SQL
- 根据查询到的相关规范内容，对生成的SQL进行校验，是否符合规范和要求
- 在你认为SQL质量无问题后，将SQL打印出来给用户看，与用户交互确认是否完成任务，等待用户确认是否结束
- 结束之前告诉用户生成的文件、工作目录在哪里。
- 当你需要写INSERT SQL语句的时候，你必须要使用wrapped_nl2sql_sub_agent_tool工具。
- 你需要保证在wrapped_nl2sql_sub_agent_tool工具调用前已经调用过metadata_recall工具。
- 你需要保证在wrapped_nl2sql_sub_agent_tool工具调用后调用过1次或多次validate_deliverables工具。
- wrapped_nl2sql_sub_agent_tool 调用时，DataTaskIR 记录的已确认口径是最高优先级业务口径：当其他业务口径描述（包括你在 query 中附加的“业务口径”段落、中间推导）与 DataTaskIR 冲突时，以 DataTaskIR 为准；不得在 query 中附加与 IR 冲突的过滤、去重、聚合口径说明。
   - query 的“已确认口径（最高优先级）”“业务口径”段落只允许包含 DataTaskIR 已记录的操作；IR 未记录去重（fact_deduplication / dimension_deduplication / final_sequence_deduplication 均无已确认内容）时，禁止在 query 中写入任何去重口径（如“同一设备同一标签只保留一条”“按 weight DESC 取最大一条”“先按键去重”等），也禁止把“源表为天级增量表/多分区/多版本”作为去重依据。
- DataOps 校验时机：在完成「8 类质量自检」并且全部 PASS 之后，必须对生成的每条 DDL/DML 调用 `dataops_validate_sql_with_log_analysis` 工具做最终校验（DDL 走 create_table 路径，DML 走 submit_job 路径）。
  - **默认行为**：当前 yaml 没有声明 `RESOURCES: dataops`，工具会自动跳过校验（返回 `passed=true, skipped=true, reason=no_dataops_resource`），等同于关闭 DataOps 校验。
  - **DataOps 提交顺序**：DDL 与 DML 必须**分两次**调用 `dataops_validate_sql_with_log_analysis`；必须先提交 DDL，DDL 返回 `passed=true` 后才提交 DML。DDL 失败时禁止提交 DML。
  - **轮询与超时**：`dataops_validate_sql_with_log_analysis` 内部固定每 30 s 轮询一次，最长等待 30 min。30 min 内未返回时返回 `timed_out=true`、跳过该 SQL 的校验并在交付报告中标注「DataOps 校验已跳过」；禁止自行加轮询或重试。
  - **校验失败处理**：启用后若 `passed=false`，从 OBS 拉取执行日志并总结错误类型 / 位置 / 修复建议，据此修正 SQL 并重新调用 `dataops_validate_sql_with_log_analysis` 校验。只有 `passed=true` 或 `skipped=true` 时才允许交付文件。
- 维度表选表经验（下面列出的维表不合适时，无需强行使用）：
  - 需要以app_id为主键的应用信息表时，天级增量表默认选biads.ads_noah_common_app_info_dm表，天级全量表默认选biads.ads_noah_hispace_app_info_ds。
  - 事实表中已存在所需要的列时，默认无需关联对应的维度表，除非用户明确要求

## SQL 规范
编写或修改任何 DDL、INSERT SQL 之前，必须先读取 `sql_rules` skill 的 SKILL.md，并据其加载 `skill/sql_rules/references/sql_rules.md` 中的适用规则（绝对禁令、SQL 方言配置、通用 SQL 生成硬规则、空值处理矩阵、SQL 构造）；生成后逐条对照自检。

## 任务路由与冲突裁决
- 目标：识别目标粒度、时间窗口、产出表、源表线索、原始 SQL/样例 SQL、用户硬约束。
- 冲突规则：用户输入、历史样例 SQL、通用规则与本 `config.yaml` 冲突时，以本配置为准。
- 输出：任务摘要、已知硬约束、不确定项清单。

### 口径冻结表（SQL 前置契约）
使用 `read_file` 读取本 skill 目录下的 `references/calibration_tables.md`，根据查询到的相关内容编写口径冻结表；表格只描述字段角色和业务规则，不绑定具体物理表名或具体字段名；每个适用任务都必须填写，不适用时写明“不适用 + 原因”。

编写完口径冻结表后、调用 `wrapped_nl2sql_sub_agent_tool` 之前，必须调用一次 `freeze_calibration_to_ir` 工具，把这份已填好的口径冻结表（连同用户原始需求 `user_query`）融入 DataTaskIR：该工具会把冻结表作为最高优先级的已确认口径写入 DataTaskIR，供后续 SQL 生成与交付校验强制遵循。若后续（如用户反馈、口径修正）冻结表内容发生变化，需用更新后的冻结表再次调用 `freeze_calibration_to_ir` 刷新 DataTaskIR。

## 最终交付内容
最终交付物使用 write_file 写入 workspace 目录下，交付内容：
- DDL 文件 `create_<table_name>.sql`（Hive 方言）
- INSERT 文件 `insert_<table_name>.sql`（Spark SQL 方言）

### DDL 文件 `create_<table_name>.sql`（Hive 方言）
路径: `<WORKSPACE>/create_<table_name>.sql`（每张目标表一个文件）
内容：仅包含一条 Hive 方言的建表语句，必须包含：
- `CREATE EXTERNAL TABLE IF NOT EXISTS {db}.{table_name} (...)`
- 库名{db}。如果库名难以确定，默认使用 biads 作为库名。
- 字段类型必须使用以下白名单；除分区列外，禁止使用 DOUBLE、FLOAT、BOOLEAN、DATE、TIMESTAMP、VARCHAR、CHAR、ARRAY、MAP、STRUCT 等未列出的类型：
   | 字段类型 | 使用范围 |
   | --- | --- |
   | STRING | 字符串型 |
   | TINYINT | 数值型 |
   | SMALLINT | 数值型 |
   | INT | 数值型 |
   | BIGINT | 数值型 |
   | DECIMAL | 小数型，必须显式声明精度与小数位，未指定时，默认使用38位精度和6位小数位，如 `DECIMAL(18,6)`，百分数同样默认使用38位精度和6位小数位 |
- `COMMENT` 字段注释（中文业务含义）
- `PARTITIONED BY (pt_* string COMMENT ...)`（分区列不在 column list 中）
- `STORED AS ORC`
- 时间窗口不同的特征，在DDL中要输出为不同的列。
- 行为类型不同的特征，在DDL中要输出为不同的列。
- 禁止在 DDL 中声明 `PRIMARY KEY`（Hive 不支持）
- 用户未要求输出的中间列不要放在DDL中

### INSERT 文件 `insert_<table_name>.sql`（Spark SQL 方言）
路径: `<WORKSPACE>/insert_<table_name>.sql`（每张目标表一个文件）
内容：仅包含一条 Spark SQL 方言的插入语句，必须包含：
- `INSERT OVERWRITE {db}.{table_name} PARTITION ...`
- SELECT 中**不输出分区列**（分区值在 PARTITION 子句指定）
- 可包含子查询（UNION ALL 预聚合模式）
- 比率字段统一分母保护：`IF(分母!=0, 分子/分母, 0)`
- 禁止使用 `INSERT OR REPLACE`（SQLite 方言，Hive/Spark 不支持）、`INSERT INTO`（非幂等）

其中 `<table_name>` 由实际目标表名决定（如 `<domain>_<subject>_<metric>_<window>_dm`）。字段类型必须与对应DDL一一对齐。

### 分表写入（Split-Table）规则
当出现以下任一情况时，可以将特征拆分到多张目标表，每张表各自配套独立的 DDL + INSERT 文件：
- 不同特征组的主键完全不同时，可以分到不同的目标表中
- 单表字段数过多（不超过80列时请勿拆分）
- 分表时，需说明不同表之间的区别。

以下情况应合并到同一张表：
- 特征维度不同但主键相同时，强制合到一张表
- 多个维度特征被同一业务场景同时使用时，强制合到一张表。

## 交付检查清单（DDL-DML 对齐 + 方言合规 + JOIN 安全）
你需要调用validate_deliverables工具完成静态的交付件检查。同时，在写入文件前，必须**逐条**自检。其中的 Gate Check 不通过则禁止写入文件。

## Final Delivery
在最后一个 step 中，必须：
1. 输出每个文件的完整路径
2. 简要总结交付内容（含审查报告摘要）
3. 不要在最终输出中重复展示任何中间版本的 SQL
4. 输出在整个特征任务开发过程中，你遇到的不确定的点，以及你做出了什么行动。要求分点说明，方便用户复核和纠正。
