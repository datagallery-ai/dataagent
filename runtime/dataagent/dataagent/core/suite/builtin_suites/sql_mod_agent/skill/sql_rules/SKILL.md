---
name: sql_rules
description: |
  当需要审核或修改生产 SQL（DDL/DML）时使用此 skill。在审核 SQL 之前查阅此 skill，检查输入 SQL 是否符合下列规则。修改后的 SQL 也要符合下列规则。
  不适用于：纯数据分析任务（不产出生产 SQL）、非 Hive/Spark 引擎的 SQL。
---

# SQL Rules（数仓生产规范）

> **前置要求**：执行本 skill 前，必须先确认已阅读并遵守 instructions.md 中的通用规则。

## 何时查阅

- 审核或修改任何 DDL/DML SQL 之前
- 审查 SQL 是否符合规范
- 需要查询 SQL 模式或约定时

## 参考文档

知识库位于 `references/de_agent/readbook/`，使用渐进式披露方式读取：

1. **第一步**：使用 `read_file` 读取 `references/de_agent/readbook/README.md`，获取完整的 chunk 路由表和读取契约
2. **后续步骤**：按 README 中的路由表，根据当前任务缺口，使用 `read_file` 逐个读取 `references/de_agent/readbook/chunks/` 下的 Markdown 文件
3. 每次只读一个 chunk，按缺口补读，不一次性读完

## SQL 约束及规范

### 绝对禁令（Zero-Tolerance，违反即交付失败）
以下规则具有最高优先级，必须在写任何 SQL 之前内化：
1. **DDL 必须包含 PARTITIONED BY**：每张目标表的 DDL 必须显式声明 `PARTITIONED BY (pt_* string COMMENT ...)`。没有 PARTITIONED BY 的 DDL 一律视为交付失败。*需要根据目标场景填写，见下文sql生成规则。
2. **DML 必须使用 INSERT OVERWRITE + PARTITION**：每条 INSERT 必须写成 `INSERT OVERWRITE {db}.{table} PARTITION ... SELECT ...`。使用 `INSERT INTO`、`INSERT OR REPLACE`、或省略 PARTITION 子句的 INSERT 一律视为交付失败。
3. **WITH/CTE 必须位于 INSERT OVERWRITE 之前**：当 INSERT 语句使用 WITH/CTE 时，无论 SparkSQL 使用什么版本，都要保证 WITH/CTE 必须位于 INSERT OVERWRITE 之前，正确示例（correct syntax）是 `WITH ... INSERT OVERWRITE ... PARTITION ... SELECT ...`。
4. **JOIN 前必须预聚合，禁止明细直连**：多事实表（或事实表与维表关联键存在重复行）场景下，每张参与 JOIN 的子表必须先按 JOIN 键 GROUP BY 聚合到唯一粒度，再做 JOIN。未预聚合的明细表直接 JOIN 导致笛卡尔积膨胀，一律视为交付失败。具体要求：
- 每个 JOIN 的两侧子查询/CTE 必须在 JOIN 键上已做 GROUP BY 或可证明唯一（如维表主键）。
- 若两张表在 JOIN 键上都有多行（N:N），必须各自先聚合到 JOIN 键粒度。
- 维表若在关联键上有重复（如一个业务对象 ID 对应多行），也必须先去重或聚合。
规则 1-2 存在因果关系：DDL 先声明了分区列，INSERT 才能引用分区。**先写 DDL、确认 PARTITIONED BY 存在，再写 INSERT。**

### SQL 方言配置（最高优先级）
本项目的 SQL 方言由 DATABASE.sql_dialect 配置决定：
- **DDL 使用 Hive SQL 方言**：必须使用 `CREATE EXTERNAL TABLE IF NOT EXISTS`、`PARTITIONED BY`、`STORED AS ORC` 等 Hive 语法
- **DML 使用 Spark SQL 方言**：必须使用 `INSERT OVERWRITE ... PARTITION ...` 语法
- **禁止本地执行验证**：你没有任何本地数据库（包括 SQLite）可供执行 SQL。不要尝试执行 DDL/DML 来验证语法，不要将 SQL 改写为 SQLite 兼容语法。SQL 的正确性通过人工审查和 Gate Check 清单保证，而非本地执行。
严禁在交付 SQL 中使用 SQLite 语法（如 `INSERT OR REPLACE`、`CREATE TABLE ... PRIMARY KEY`、无 `PARTITIONED BY` 的建表语句）。
当前 SparkSQL 使用的是 3.1 的版本，所以请保证你输出的 SQL 语法是符合这个版本标准的，且额外必须保证WITH/CTE 位于 INSERT OVERWRITE 之前。

### 通用 SQL 生成硬规则（从历史问题抽象）
这些规则只描述可复用的工程模式，不绑定具体表名或具体业务列名。生成 SQL 时必须优先套用，除非用户给出更高优先级的明确业务约束。
1. **要求排序的序列特征必须先定序再拼接**：
   - 生成对象序列、TopN、最近行为序列时，先用 `POSEXPLODE` 保留原始位置，或用 `ROW_NUMBER() OVER(PARTITION BY ... ORDER BY ... DESC)` 生成排序号，再使用 `bicoredata.ConcatWithRank(value, rank_col, separator)` 拼接。
   - 禁止用 `CONCAT_WS(',', COLLECT_LIST(x ORDER BY ...))` 作为最终序列实现；该写法在目标 Spark/Hive 环境中排序稳定性和兼容性不足。
   - 禁止用 `CAST(x * 10000 AS INT)` 以及类似方式处理类型为小数的排序字段作为 `bicoredata.ConcatWithRank` 的排序号输入，必须使用窗口函数排序生成排序号。
   - 排序字段可能并列时，追加稳定 tie-breaker（如业务对象 ID、列表位置、事件唯一标识），或先聚合到唯一业务粒度后再排序。
   - 排序号的 `PARTITION BY` 必须匹配业务序列口径：若目标是在某个派生维度下输出主体行为序列，应先确认序列是按主体全局定序后切分，还是按该维度内重新定序；不要默认把最终输出维度全部加入排序分区。
   - 不定义行为序列口径的伴随字段、派生字段或存储分片字段（e.g. `<shard_key>`、`<bucket_key>`、`<display_label>`），不应被随意加入排序 `PARTITION BY` 造成序列重排或切碎。
   - 对多个同粒度序列块进行横向合并时，每个序列块先独立聚合到完整输出主键粒度，再用完整主键合并；禁止先按主体聚合成键值对字符串后再输出。
   - 不要求排序的序列不需要定序，基于是否要求去重优先使用 `CONCAT_WS(',', COLLECT_LIST(x))` 或 `CONCAT_WS(',', COLLECT_SET(x))` 来拼接。
2. **业务维度必须作为列保留**：
   - 需求要求按“主体 ID + 分类维度 / 场景维度 / 派生维度”等粒度输出时，所有业务维度必须出现在 SELECT 和 GROUP BY 中。
   - 禁止把应作为列的维度编码进一个字符串字段，导致目标表缺少独立维度列。
3. **输出格式是业务口径，不是实现细节**：
   - 序列分隔符、键值对格式、字段是否拆列、目标粒度、TopN 截断规则必须来自用户需求、样例 SQL、规范手册或元数据证据，并写入需求表。
   - 禁止凭习惯选择分隔符或输出格式；多个证据冲突或只有问题标注提示时，必须进入 HITL 或沿用更高优先级证据。
4. **分类维度空值默认过滤，不默认填占位值**：
   - 通过维表补齐分类、标签、层级属性后，若该属性参与分组或作为目标字段，空值默认使用 `WHERE !bicoredata.IsEmpty(field)` 过滤。
   - 禁止将关键业务分类补成占位值（e.g. `'<unknown>'`、`'<empty>'`）后继续入表，除非用户明确要求保留未知分类并说明下游含义。
5. **平台 UDF 必须优先于通用函数**：
   - 空值判断使用  `bicoredata.IsEmpty`，不要用 `IS NOT NULL AND <> ''` 替代关键字段判断。
   - 设备 ID 合法性过滤使用 `bicoredata.isDeviceIdLegal(device_id)`。
   - SHA256 加密使用 `upper(bicoredata.SHA256(upper(raw_id)))` 这类平台 UDF，不要自行换成通用 `SHA2`。如果用户没有明确指定，优先使用 `bicoredata.SHA256`。
   - 序列拼接优先使用 `bicoredata.ConcatWithRank`，不要自行用不稳定的 `collect_list` 排序拼接。
   - JSON字符串解析优先使用平台UDF `bicoredata.getJsonObject`，不要使用通用的 `get_json_object`。
   - 使用平台UDF函数 `bicoredata.DateFormat` 做日期格式转换时，只能传入日期字符串一个参数。
6. **字段名和映射方向必须以 schema/元数据证据为准**：
   - 遇到相近字段、同义字段、层级分类字段、映射表左右字段时，必须先用元数据证据确认真实字段名和映射方向。
   - 禁止凭业务语义臆造字段名，或把映射表的输入列、输出列方向写反。
   - 使用映射/扩展维表时，必须区分“事实表输入维度”“映射表匹配键”“映射表输出维度”（e.g. `<source_dim>` JOIN `<map_from>`，输出 `<map_to>`）：JOIN 应连到映射键，目标派生维度应取映射输出列；若输出维度为空，默认过滤，不要回退到输入维度冒充派生维度。
7. **去重口径先于派生键生成**：
   - 去重、取最近、TopN 的 `PARTITION BY` 应优先使用源表中的原始业务键和原始对象键；派生标识、分桶字段、展示字段、标签字段等（e.g. `<hashed_key>`、`<bucket_key>`、`<label_name>`）只在去重后生成或用于最终输出。
   - 只有在元数据或业务证据证明派生字段与原始键一一等价时，才允许用派生字段替代原始键参与去重。
   - 生成派生键时，源字段为空或不合法必须先过滤或进入 HITL，禁止让空派生键继续作为目标主键、分组键或序列分组键。
8. **维表快照必须去重到 JOIN key 粒度**：
   - 多分区、多版本或一键多行维表，不能简单 `GROUP BY join_key, attribute` 当作唯一；应在业务窗口内按 JOIN key 用窗口函数取最近/最可信一条，或按 JOIN key 聚合到唯一属性。
   - 任何不能证明 JOIN key 唯一的维表，JOIN 前必须去重或聚合到唯一粒度。
9. **时间窗口使用调度变量表达**：
   - 从分区表中读取数据时，一般需要按分区进行过滤，避免全量读取带来的性能问题。
   - 如果用户没说，默认的分区粒度就是天。如果用户说了，就按用户说的粒度分区，常见的表分区粒度：天分区、小时分区。
   - 如果是维表中的天级增量表（_dm 后缀），用户未明确要求时，需要结合任务的语义判定取当天数据还是取多天数据。在取多天数据时，要对数据做去重，数据重复时一般保留最新分区的数据即可。
   - 如果是维表中的天级全量表（_ds 后缀），一般只要取 pt_d = '$date' 即可。
   - 近 N 天窗口包含当天(共 N 天, 当天在内)的写法参考:
      - 写法A(起始端开放, 终止端闭合): pt_d <= '$date' AND pt_d > '$ {start_time,-N,yyyyMMdd}'
        示例: N=7 → pt_d <= '$date' AND pt_d > '$ {start_time,-7,yyyyMMdd}'
      - 写法B(起始端闭合, 终止端闭合): pt_d <= '$date' AND pt_d >= '$ {start_time,-N+1,yyyyMMdd}'
        示例: N=7 → pt_d <= '$date' AND pt_d >= '$ {start_time,-6,yyyyMMdd}'
   - 近 N 天窗口不包含当天(共 N 天, 当天不在内)的写法参考：
      - 写法A(起始端闭合, 终止端开放): pt_d < '$date' AND pt_d >= '$ {start_time,-N,yyyyMMdd}'
        示例: N=7 → pt_d < '$date' AND pt_d >= '$ {start_time,-7,yyyyMMdd}'
      - 写法B(起始端开放, 终止端开放): pt_d < '$date' AND pt_d > '$ {start_time,-N-1,yyyyMMdd}'
        示例: N=7 → pt_d < '$date' AND pt_d > '$ {start_time,-8,yyyyMMdd}'
10. **使用间隙和孤岛算法计算连续日期**：
   - 按升序或者降序来选择其中一种算法即可
      - 使用 `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ... DESC) AS rn` 为日期排序计算出倒序排序号，再使用 `DATE_ADD(..., rn) AS grp_date` 计算日期和排序号之和，最后再根据和值做分组获得连续日期区间。
      - 使用 `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ... ASC) AS rn` 为日期排序计算出升序排序号，再使用 `DATE_SUBTRACT(..., rn) AS grp_date` 计算日期和排序号之和，最后再根据和值做分组获得连续日期区间。
   - 计算日期和行号之和时不能将日期 CAST 为数字再直接与排序号求和，因为数字求和和日期求和不一定等价。
   - 使用DATE_ADD时需要注意日期格式，例如使用 `pt_d` 做计算时需要先使用 `bicoredata.DateFormat` UDF将其从 `yyyyMMdd` 格式转换为 `yyyy-MM-dd` 格式。
11. **宏表达式中绝对不能出现算术运算**:
   - 宏表达式的偏移量必须是数字常量，不支持算术运算（如 -5*365 需预计算为 -1825）。
12. **遵循以下按时间进行的表分区规则**：
   - 所有的表分区必须以 `pt_` 开头，时间周期分区分别为 `pt_i`(分)，`pt_h`(时)，`pt_d`(天)，`pt_w`(周)，`pt_m`(月)，`pt_y`(年)，业务分区为 `pt_service`，其他根据实际需要自定义
   - 示例：
      - 小时分区：（DDL中）`PARTITIONED BY (pt_d string COMMENT '天分区', pt_h string COMMENT '小时分区')`，（DML中）INSERT OVERWRITE TABLE ... PARTITION(pt_d = '$date', pt_h = '$hour')
      - 天分区：（DDL中）`PARTITIONED BY (pt_d string COMMENT '天分区')`，（DML中）INSERT OVERWRITE TABLE ... PARTITION(pt_d = '$date')
      - 周分区：（DDL中）`PARTITIONED BY (pt_w string COMMENT '周分区')`，（DML中）INSERT OVERWRITE TABLE ... PARTITION(pt_w = '$monday_ep')
      - 月分区：（DDL中）`PARTITIONED BY (pt_m string COMMENT '月分区')`，（DML中）INSERT OVERWRITE TABLE ... PARTITION(pt_m = '$month')
13. **目标表分区粒度推导规则**：
   - 生成目标表时，必须先确定目标表的时间粒度。目标表时间粒度来源按优先级排序：
      - 用户明确指定的粒度，如按分钟统计、按小时统计、按天统计、按周统计、按月统计
      - 用户指定的目标表名后缀，如*_im → 分钟表、*_hm → 小时表、*_dm → 天表、*_wm → 周表、*_mm → 月表
      - 用户未指定时，默认采用天分区表(pt_d)
   - 目标表分区必须与最终产出数据的刷新粒度一致，而非简单继承源表分区。
14. **时间粒度与分区定义映射**
   - 分钟级：PARTITIONED BY (pt_d STRING, pt_h STRING, pt_i STRING)
   - 小时级：PARTITIONED BY (pt_d STRING, pt_h STRING)
   - 天级：PARTITIONED BY (pt_d STRING)
15. **DDL与DML分区一致性约束**
   - DDL中的PARTITIONED BY列集合 必须等于 INSERT OVERWRITE中的PARTITION列集合。
   - 例如 DDL:PARTITIONED BY (pt_d, pt_h)，则DML必须：PARTITION(pt_d='$date',pt_h='$hour')，否则SQL非法。
16. **依赖链顺序规则**
   - 定义必须先于引用：任何子查询、CTE (WITH)、临时表在被 SELECT/JOIN/INSERT 引用前，必须已经定义。
   - 典型错误模式：主语句（INSERT/UPDATE/MERGE）放在子查询/CTE 定义之前，导致解析器无法识别子句中定义的列，产生 `cannot resolve 'xxx' given input columns` 错误。
17. **维度表字段溯源**
   - 生成 SQL 前先确认维度表的 JOIN key 是什么格式（原始ID 还是 转换后ID）
   - ON 条件左右两边的字段必须一致
18. **聚合维度完整**
   - 用户问题中明确列出的所有分组维度（GROUP BY字段）必须全部包含在SQL中
   - 不可以省略用户问题中提到的任何分组维度

### 空值处理矩阵（必须先定策略再写 SQL）
所有判空逻辑必须写进需求表的“空值/去重策略”，并在自检中逐项核对。不要把所有空值都机械地 `COALESCE`，也不要只用 `IS NOT NULL`。
先按字段角色选择以下策略之一：
- **过滤**：主键、JOIN key、业务对象、分类/映射输出、序列对象等缺失会改变粒度或语义的字段，默认过滤，除非用户明确说明不要过滤。
- **合法回退派生**：同一业务实体有优先级字段和备用字段时，可按证据定义 `if(<primary>可用, <primary>, derive(<backup>))`，但备用字段必须先通过空值/合法性校验，派生函数必须用平台 UDF。
- **证据化保留**：若参考 SQL、样例 SQL、上游规范或 schema 说明字段已清洗，或需求要求保留无明细主体，不要擅自新增过滤；必须在需求表说明保留依据和下游含义。
- **省略过滤也需要解释**：当可信参考逻辑只过滤分区/窗口而不额外过滤目标键或排序字段时，应先确认这是上游质量保证、业务保留要求，还是遗漏；不要因为字段出现在 SELECT/GROUP BY 就自动添加判空。
1. **主键 / 粒度字段默认过滤**：
   - 目标表主键、分组粒度、JOIN key、设备 ID、业务对象 ID、分类维度、派生维度为空时，默认过滤该行。
   - 字符串字段使用 `!bicoredata.IsEmpty(field)` 判断；不要只写 `field IS NOT NULL`，因为空字符串也应视为空。
   - 对 JOIN key，必须在 JOIN 前过滤或归一化空值，避免空 key 造成数据倾斜、错误匹配或无效输出。
   - 若上游表、样例 SQL 或数据规范已经证明字段在源表中清洗完成，不要机械追加无证据过滤条件；任何可能改变保留行数的额外过滤都必须写明证据，证据不足时进入 HITL。
2. **设备 ID 必须合法性优先**：
   - 以原始设备 ID 作为用户识别依据时，过滤条件必须包含 `bicoredata.isDeviceIdLegal(device_id)` 或经元数据证明确认的等价合法性 UDF。
   - 生成加密设备 ID 时，优先复用上游已加密字段；需要回退加密原始设备 ID 时，必须先确认原始设备 ID 合法，再使用平台 SHA256 UDF。
   - 派生分桶键、哈希键、尾部标识等主键组成部分时，源字段不可用则过滤，不要输出空派生键作为主键。
   - 多字段回退必须保持优先级清晰：先定义主字段、备用字段、合法性条件和派生方式，再在 SELECT、去重、分组、主键生成中复用同一口径。不要把回退逻辑拆散到多个不一致的 CASE / PARTITION BY 中。
3. **维度补属性不填假分类**：
   - 禁止使用占位字面量、空字符串或泛化“未知值”（e.g. `'<unknown>'`、`'<other>'`）作为分类入表，除非需求明确要求保留未知分类并说明下游含义。
4. **指标类字段可填充，但必须按语义填充**：
   - 计数/金额/时长等可加性指标，在 LEFT JOIN 或 UNION ALL 补齐宽列时可 `COALESCE(metric, 0)`，但必须在需求表说明“缺失表示无行为”。
   - 比率类字段不要直接 `COALESCE(rate, 0)` 掩盖分母为空；必须使用 `IF(denominator > 0, numerator / denominator, 0)`。
   - 日期、时间、状态码、分类标签等非可加字段默认不补 0；缺失时过滤或 HITL。
5. **序列字段空值策略**：
   - 参与序列拼接的业务对象为空时，先过滤；空对象不得进入 `ConcatWithRank`。
   - `HAVING seq IS NOT NULL AND seq != ''` 这类最终判空应优先改为上游对象过滤 + `ConcatWithRank` 结果校验，避免掩盖明细质量问题。
6. **判空函数大小写和证据**：
   - UDF 手册中有 `IsEmpty` / `isDeviceIdLegal` 线索；实际函数名、大小写、参数必须通过 `metadata_recall` 确认。
   - 同一条 SQL 中判空 UDF 大小写尽量保持一致；若沿用参考 SQL 的大小写，需在说明证据来源。

### SQL 构造
- DDL 使用 Hive：`CREATE EXTERNAL TABLE IF NOT EXISTS`、字段 `COMMENT`、`PARTITIONED BY (pt_* string COMMENT ...)`、`STORED AS ORC`。
- DML 使用 Spark：`INSERT OVERWRITE {db}.{table} PARTITION ... SELECT ...`，SELECT 中不输出 `pt_d` 等时间分区列。
- 多事实合并优先使用 UNION ALL 预聚合模式；禁止明细 N:N 直接 JOIN。
- 普通字段类型只使用 `STRING`、`TINYINT`、`SMALLINT`、`INT`、`BIGINT`、`DECIMAL(p,s)`。
- 字符串类型的列，如果实际存储的数据为整数或浮点数，可在 SELECT 表达式中 CAST 为 DOUBLE 后再参与 SUM/AVG 计算；DOUBLE 仅作为过程计算的中间类型，DDL 列类型仍必须使用上一条的白名单（落表前再按本模块下文的精度规则 CAST 为 DECIMAL(p,s)）。
- 选用聚合函数 AVG / MIN / MAX / SUM 时应该结合字段的名称和描述，例如平均值字段使用AVG，总值字段使用SUM，最大值字段使用MAX，最小值字段使用MIN。当对于已采样的样本进行聚合时，要考虑原始表列语义和聚合函数的搭配使用，如已有列语义已经是最大最小值，且目标计算结果仍然是最大最小值，那么通常来说应该要对各样本的最大值取最大值，各样本的最小值取最小值。
- 过滤时间时优先使用分区时间字段，除非用户显式地要求使用其他字段过滤。
- 用户没有明确要求时不要使用ROUND，保留小数应该使用 `CAST (x as DECIMAL(n,d))`。特征为比率时默认使用 `CAST (x as DECIMAL(26,2))` 来保留两位小数，除非用户有其他显式要求。用户没有明确要求时，除了比率外的特征不要用 CAST / ROUND 修改精度。
- 禁止 `INSERT INTO`、`INSERT OR REPLACE`、SQLite `PRIMARY KEY` 语法。
- 输出：DDL 草案、INSERT 草案。
- JOIN 类型：明确 JOIN 的业务类型。事实表关联维表必须使用 LEFT JOIN，任何情况下（包括过滤空值或源表已聚合时）都不能使用 INNER JOIN，并且不要对维表字段做空值过滤使 LEFT JOIN 退化为 INNER JOIN，这是为了避免丢弃事实表中未匹配的记录。
