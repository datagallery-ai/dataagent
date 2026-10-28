---
name: feature_engineering
description: |
  从零开发数仓特征表。适用于根据用户需求创建新的特征表，并生成配套的 DDL（建表）与 INSERT（插入）SQL 的场景。
  不适用于：给已有表新增列（走 add_columns skill）、纯 SQL 语法规范（见 sql_rules skill）。
---

# 特征开发

根据用户的需求进行特征开发。计划内容包含但不仅限于：
- 查询相关的元数据、表、知识、命名规范等信息
- 根据查询到的相关内容，编写口径冻结表（仅作写 SQL 的前置契约，不写入 / 不驱动 DataTaskIR）
- 编写DDL/INSERT的SQL, 必须先写DDL，再写INSERT SQL
- 根据查询到的相关规范内容，对生成的SQL进行校验，是否符合规范和要求
- 在你认为SQL质量无问题后，将SQL打印出来给用户看，与用户交互确认是否完成任务，等待用户确认是否结束
- 结束之前告诉用户生成的文件、工作目录在哪里。
- 当你需要写INSERT SQL语句的时候，你必须要使用wrapped_nl2sql_sub_agent_tool工具。
- 你需要保证在wrapped_nl2sql_sub_agent_tool工具调用前已经调用过metadata_recall工具。
- **历史SQL检索（强制步骤）**：schema linking 完成后、调用 wrapped_nl2sql_sub_agent_tool 之前，你必须再调用一次 metadata_recall 检索相似历史SQL，把检索结果作为过滤口径、列用法、UDF 用法的参考。步骤：
   1. 从口径冻结表提取⌈输出特征名称表⌋（第 9 项）与目标粒度（本次输出主键）。
   2. 对每个输出特征标注用户行为（曝光/点击/下载/激活/使用/卸载/通话/搜索/推送/计费/注册…，依据特征名、业务目标与源表语义）。
   3. 一次调用 metadata_recall 携带全部特征（该工具要求整合后单次调用），query 按模板书写：
      "检索历史SQL作为参考，要求：输出特征1：<特征名>（用户行为：<行为>）；…；本次输出主键：<主键字段组合>；加分项：<特征类型/时间周期/排序维度>；源表必须包含：<已确认源表>（源表已确认时填写）"
   4. 检索有匹配时，wrapped_nl2sql_sub_agent_tool 的 query 中**必须**包含⌈## 历史SQL参考⌋段落：写入子代理返回的过滤口径摘录、列用法摘录、UDF 用法摘录（合计不超过 2000 字符），并注明"历史SQL仅供参考，与已确认口径/DataTaskIR 冲突时以 DataTaskIR 为准"；无匹配时如实说明"未找到相似历史SQL"，query 不带该段落；工具返回"历史SQL检索功能未启用"时，说明当前部署未开启该能力（并非"已检索但无匹配"），直接跳过历史SQL检索与引用，query 不带该段落。
   - 历史SQL检索必须委托 metadata_recall 子代理完成，**禁止**自己用read_file/grep/glob 直接翻历史SQL目录文件。
   - 自由调用：多个候选源表且元数据无法定夺时，可在选表阶段提前调用 metadata_recall，请求改为"检索使用了表 X/Y/Z 的历史SQL并对比它们在各历史SQL中的实际用法（过滤口径、列用法、粒度）"，用返回证据辅助选表。
- 你需要保证在 wrapped_nl2sql_sub_agent_tool 工具调用后，至少调用过 1 次 `check_and_repair_sql`。
- **数据探查**（写 SQL 前确认字段真实存在 / 取值分布 / 空值情况）：
  - 何时调、调用硬约束（`partition_filter` / `columns` / 占位符）、返回结果解读、工具签名，详见 `skill/dataops_probe/SKILL.md`。
  - 调用入口：`dataops_probe_table`，仅 SELECT LIMIT，失败自动跳过不抛错。
  - 与 metadata_recall 的分工：metadata_recall 回答"表/列是什么"，dataops_probe_table 回答"字段实际长什么样"。
- wrapped_nl2sql_sub_agent_tool 调用时，DataTaskIR 记录的已确认口径是最高优先级业务口径：当其他业务口径描述（包括你在 query 中附加的“业务口径”段落、中间推导）与 DataTaskIR 冲突时，以 DataTaskIR 为准；不得在 query 中附加与 IR 冲突的过滤、去重、聚合口径说明。
  - query 的“已确认口径（最高优先级）”“业务口径”段落只允许包含 DataTaskIR 已记录的操作；IR 未记录去重（sources 事实/维度 dedup、sequence.dedup 均无已确认内容）时，禁止在 query 中写入任何去重口径（如“同一设备同一标签只保留一条”“按 weight DESC 取最大一条”“先按键去重”等），也禁止把“源表为天级增量表/多分区/多版本”作为去重依据。
  - 历史SQL检索结果是参考级证据，不是口径来源：其时间窗口、源表选择、过滤条件、聚合实现不得写入 query 的“已确认口径”“业务口径”段落，不得据此改写或补充 IR 已记录/未记录的任何口径，也不得作为口径冻结表的口径依据（freeze_calibration_to_ir 冻结的口径只能来自用户原始需求与已确认上下文）；用户原话与 IR 均无对应口径时保持留空，禁止把历史SQL 的口径当作默认值填充。历史SQL 的正确用法是写法参考：UDF 调用形态、列用法、序列拼接方式只放在⌈## 历史SQL参考⌋段落传达，并保留“仅供参考，与已确认口径/DataTaskIR 冲突时以 DataTaskIR 为准”的声明。
  - 最终输出列语义 = 已确认特征主键（identity.components，必选前缀，即使未出现在 output_features 中）+ expand(output_features) 业务/指标语义；多时间窗口 × 多特征等交叉场景必须以 IR 中确定性展开后的字段条数为准（如 3×3=9），不得在 query 中压缩成更少的“代表列”。IR 给出的展开列名（field_name / name_pattern）仅供主 Agent 对照参考，query/SQL/DDL **不要求**照抄同名。
  - **主键/分组键防重复**：identity.components 是主键列唯一权威；禁止在 output_features（measures/extras/features）再写同名输出列。aggregation.grain 可引用主键 field_ref（表示按该键聚合），这不等于还要再输出一列同名键。query 中也不要把主键既当“已确认主键前缀”又当“业务特征列”各写一遍。
  - **仅当用户明确要求不要去重时**：IR 的 `aggregation.grain` 与 sources/sequence dedup 均应保持未确认（null）；禁止因“每个应用/设备”或 identity 在 query/SQL 中 invent GROUP BY 折叠、DISTINCT、ROW_NUMBER 去重。这是特例，不是默认。
  - **常规聚合（默认）**：用户多数情况下不会显式说“要聚合/GROUP BY”；只要有求和/计数/均值等聚合语义，IR 应照常填写 grain，query/SQL 按已确认 grain 实现。禁止把“禁止去重时不填 grain”误用成漏填聚合粒度。
  - 若展开后各特征 aggregation.grain 不同，query/SQL 必须按列分别说明并实现各自聚合粒度；禁止用单一全局 GROUP BY 覆盖全部特征。
- **SQL checker**：DDL/DML 写完后调用 `check_and_repair_sql`，只按它返回的 issues 修 SQL；PASS 才继续，BLOCKED/ERROR 不得当作通过。
  - checker 做确定性规则检查和有界模型修复，最多 6 次模型调用；不要用自然语言推断替代规则。
  - DDL 与对应 DML 必须作为同一交付单元，DDL 未通过不得继续提交 DML。
- **DataOps 校验（checker 内部阶段）**：由 `check_and_repair_sql.config.dataops_validation_enabled` 控制，默认开启；仅需静态检查时设为 `false`。
  - 开启后，程序规则检查通过才会在 DataOps 临时表上按 DDL → DML 顺序校验；DDL 失败不提交 DML。
  - 失败返回 BLOCKED 及 `dataops.results` 中的日志分析摘要；处理问题后重新调用 checker。同一交付文件对每条 SQL 最多提交 3 次，修改 SQL 不重置预算。
  - 开启但服务未配置/校验被跳过时不得当作通过；配置关闭时报告 DISABLED，不声称完成 DataOps 验证。
  - checker 未通过时不自动发布候选补丁。
- 维度表选表经验：
  - 事实表中已存在所需要的列时，默认无需关联对应的维度表，除非用户明确要求。例如事实表中已提供应用三级分类时，默认无需使用应用信息维表。

## SQL 规范
编写或修改任何 DDL、DML SQL 之前，必须先读取 `sql_rules` skill 的 SKILL.md，按要求加载对应的 SQL 编写规范（DDL 读 `ddl_rules.md`，DML 读 `dml_rules.md`）；生成后逐条对照自检。
- **字符串类型空值判断 → `bicoredata.IsEmpty(field)`**（保留侧 `!bicoredata.IsEmpty(field)`）：仅针对字符串存储；禁止对 STRING 用 `IS NULL` / `IS NOT NULL` / `= ''` / `<> ''` 替代。**语义非字符串但按字符串存储**（时间/次数/金额等存 STRING）仍必须用 `bicoredata.IsEmpty`；**非字符串类型存储**（元数据为数值/时间戳等）不适用 `bicoredata.IsEmpty`，按该类型判空。
- **禁止方言不支持的类型**：DDL 列类型与 `CAST` 目标类型必须属于 Hive/Spark 白名单；禁止 `REAL`/`NUMERIC`/`TEXT` 等跨方言类型（过程浮点用 `DOUBLE`，落表用 `DECIMAL`）。
- **小数连算禁止中间 CAST 截断**：先算完整表达式再对最终值 CAST；禁止 `A * CAST(B/C AS DECIMAL(...))` 这类对中间结果提前 CAST，应写 `CAST(A * B / C AS DECIMAL(...))`（细则见 `dml_rules.md`）。

## 任务路由与冲突裁决
- 目标：识别目标粒度、时间窗口、产出表、源表线索、原始 SQL/样例 SQL、用户硬约束。
- 冲突规则：用户输入、历史样例 SQL、通用规则与本 `config.yaml` 冲突时，以本配置为准。
- 输出：任务摘要、已知硬约束、不确定项清单。

### 口径冻结表（SQL 前置契约）
使用 `read_file` 读取本 skill 目录下的 `references/calibration_tables.md`，根据查询到的相关内容编写口径冻结表；表格只描述字段角色和业务规则，不绑定具体物理表名或具体字段名；每个适用任务都必须填写，不适用时写明“不适用 + 原因”。

口径冻结表只服务你自己编写/校对 SQL，**不得**当作 DataTaskIR 的输入或已确认口径来源；禁止把冻结表内容写入 query 的「已确认口径」段落去覆盖 IR。DataTaskIR 仅由常规 IR 刷新路径（如 `metadata_recall` 等证据）生成与更新。

## 最终交付内容
最终交付物使用 write_file 写入 workspace 目录下，交付内容：
- DDL 文件 `create_<table_name>.sql`（Hive 方言）
- INSERT 文件 `insert_<table_name>.sql`（Spark SQL 方言）

### DDL 文件 `create_<table_name>.sql`（Hive 方言）
路径: `<WORKSPACE>/create_<table_name>.sql`（每张目标表一个文件）
内容：仅包含一条 Hive 方言的建表语句，必须包含：
- `CREATE EXTERNAL TABLE IF NOT EXISTS {db}.{table_name} (...)`
- 库名{db}。如果库名难以确定，默认使用 biads 作为库名。
- 字段类型必须使用以下白名单；除分区列外，禁止使用 DOUBLE、FLOAT、REAL、BOOLEAN、DATE、TIMESTAMP、VARCHAR、CHAR、ARRAY、MAP、STRUCT、NUMERIC 等未列出的类型：
   | 字段类型 | 使用范围 |
   | --- | --- |
   | STRING | 字符串型 |
   | TINYINT | 数值型 |
   | SMALLINT | 数值型 |
   | INT | 数值型 |
   | BIGINT | 数值型 |
   | DECIMAL | 小数型，必须显式声明精度与小数位，未指定时，默认使用38位精度和6位小数位，如 `DECIMAL(38,6)`，百分数同样默认使用38位精度和6位小数位 |
- `COMMENT` 字段注释（中文业务含义）
- `PARTITIONED BY (pt_* string COMMENT ...)`（分区列不在 column list 中）
- `STORED AS ORC`
- 时间窗口不同的特征，在DDL中要输出为不同的列。
- 行为类型不同的特征，在DDL中要输出为不同的列。
- 禁止在 DDL 中声明 `PRIMARY KEY`（Hive 不支持）
- 禁止使用当前方言不支持的类型（如 Hive 的 `REAL`）做列声明或 `CAST(... AS ...)`；INSERT 中过程浮点可用 `DOUBLE`，落表对齐 DDL 白名单
- 用户未要求输出的中间列不要放在DDL中

### INSERT 文件 `insert_<table_name>.sql`（Spark SQL 方言）
路径: `<WORKSPACE>/insert_<table_name>.sql`（每张目标表一个文件）
内容：仅包含一条 Spark SQL 方言的插入语句，必须包含：
- `INSERT OVERWRITE {db}.{table_name} PARTITION ...`
- SELECT 中**不输出分区列**（分区值在 PARTITION 子句指定）
- 可包含子查询（UNION ALL 预聚合模式）
- 比率字段统一分母保护：`IF(分母!=0, 分子/分母, 0)`
- 禁止使用 `INSERT OR REPLACE`（SQLite 方言，Hive/Spark 不支持）、`INSERT INTO`（非幂等）
- `CAST` 目标类型必须是 Spark/Hive 支持且符合规范的类型；禁止 `CAST(... AS REAL)` 等跨方言类型

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
你需要调用 `check_and_repair_sql` 完成静态的交付件检查。同时，在写入文件前，必须**逐条**自检。其中的 Gate Check 不通过则禁止写入文件。

## Final Delivery
在最后一个 step 中，必须：
1. 输出每个文件的完整路径
2. 简要总结交付内容（含审查报告摘要）
3. 不要在最终输出中重复展示任何中间版本的 SQL
4. 输出在整个特征任务开发过程中，你遇到的不确定的点，以及你做出了什么行动。要求分点说明，方便用户复核和纠正。
