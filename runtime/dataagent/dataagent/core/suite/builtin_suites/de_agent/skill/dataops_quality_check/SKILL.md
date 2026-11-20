---
name: dataops_quality_check
description: |
  Use when (任一即触发):
    1. 数据试算 —— 需要把写好的 INSERT 真正跑一遍看看产出对不对;
    2. **用户输入「执行数据校验」** —— 交付后用户显式要求校验数据质量,
       调用 `dataops_validate_data` 工具(它走同一条试算+校验+自修正链路);
    3. 单独调用数据验证 —— 只想对已有临时表/结果做数据正确性校验,不跑试算。
  能力:空值覆盖率(non-null rate) + 单一值覆盖率(single-value ratio)
    + 异常值占比(outlier ratio,仅比率类字段)
    + **数据正确性四规则**:主键重复率必须为 0 / 计数型字段必须 ≥ 0 /
    常识违规检测 / 抽样回源比对;校验出错自动**自修正**。
    前两者是可扩展校验器里的条目,新增第五种校验只需注册一个 QualityChecker。
  实现:`actions/tools/local_tool/dataops_quality_check.py`(校验器注册表 +
    SQL 生成 + 结果解析),流程型校验在 `dataops_common_sense.py` /
    `dataops_source_reconciliation.py`,自修正在 `dataops_self_correct.py`。
  NOT for: 字段是否存在/取值范围探查(那是 dataops_probe 的活);
    SQL 语法/规范校验(那是 validate_deliverables 的活);
    **只想对真实目标表做体检、不建表不插入**(那是 dataops_validate_data 的活)。
---

# 数据正确性校验 (Data Quality Check)

## 核心心智模型

一条链路,一个触发点,一套校验器:

```
  ①用户显式触发                        ②主动调用
    dataops_validate_data                 trial_compute_table
        └─ trial_compute_table                  └─ 重建临时表(按 create_*.sql)
               └─ 重建临时表                            │
                      └─ 试算到临时表                     └─ 试算 → 校验 → DROP
                              └─ run_quality_check() ← 同一个函数 ─────┘
```

校验器注册表 `_BUILTIN_CHECKERS`（可扩展）：`null_rate`（空值覆盖率）、
`single_value_ratio`（单一值覆盖率）、`outlier_ratio`（异常值占比，仅比率类字段）。

**静态校验链路(`validate_deliverables` → `dataops_validate_sql_with_log_analysis`)
不执行数据正确性校验** —— 那条链路要保持"快",好让 agent 快速迭代修 SQL;
昂贵的采样统计统一放到**用户显式触发**后执行一次。

**校验不再自动跑,改成用户显式触发** —— 交付后自动试算会把每一轮交付拖住
几分钟（试算要连 DataOps 跑一遍）。现在由 agent 在交付说明的最后一句提示
「输入『执行数据校验』执行数据校验」,用户输入后 agent 调用
`dataops_validate_data` 工具走同一条试算 + 校验 + 自修正链路。

**临时表生命周期(两条链路各自独立):**

| | 语法校验链路 | 质量校验链路 |
|---|---|---|
| 分区日期 | 一年之后(`today+365`) | 一周之前(`today-7`) |
| 临时表名 | `tmp_<user>_<日期>_<表>` | 同上,但日期不同 → **不是同一张表** |
| 建表 | DDL 步骤建 | **质量校验前按 `create_*.sql` 再建一次** |
| 删表 | DML 成功时删(DDL 的表留给同轮 DML 用) | **不管成功与否都删** |

所以质量校验链路必须自己重建临时表 —— 语法校验链路那张表既被删了,日期也不同,
根本不是同一张表。

试算与校验**共用**临时表命名(改写/分区日期)和执行路由(生产/联调),
所以试算结果和校验结果不可能漂移。

## 试算写入量会被截断(这正是试算的意义)

质量校验只对产出数据做 **`LIMIT sample_size`** 的采样统计(默认 10000 行)。
写入超过 10000 行的那部分数据**永远不会被校验看到**,所以试算的 INSERT 会加上
`LIMIT N`(N = `quality_check.sample_size`),省掉全量写入的耗时。

**校验结论不变**:临时表里前 N 行与全量写入的前 N 行**完全是同一批行**,
校验读到的样本一模一样,变的只有试算耗时。

举例,交付物 `insert_ads_user_profile_ds.sql` 是:

```sql
INSERT OVERWRITE TABLE ads_user_profile_ds PARTITION (pt_d = '${date}')
SELECT
  udid_sha256,
  bicoredata.SHA256(udid) AS udid_sha256,
  CASE WHEN pay_cnt > 0 THEN CAST(pay_cnt AS DOUBLE) / total_cnt ELSE 0 END AS pay_ratio
FROM dws.src_user_profile_dm
WHERE pt_d = '${date}'
```

试算时实际提交给 DataOps 的是(表名换成临时表、分区日期换成分区校验日期,
末尾多了 `LIMIT 10000`):

```sql
INSERT OVERWRITE TABLE adhoctemp.tmp_neizha_20261022_ads_user_profile_ds
PARTITION (pt_d = '20261022')
SELECT
  udid_sha256,
  bicoredata.SHA256(udid) AS udid_sha256,
  CASE WHEN pay_cnt > 0 THEN CAST(pay_cnt AS DOUBLE) / total_cnt ELSE 0 END AS pay_ratio
FROM dws.src_user_profile_dm
WHERE pt_d = '20261022'
LIMIT 10000
```

实现见 `dataops_validate_tool._limit_insert_rows`,特点:

- **纯文本插入**:只在查询末尾插一段 `LIMIT N`,SQL 其余部分**逐字节保持原样**
  (平台 UDF `!bicoredata.IsEmpty`、注释、`LATERAL VIEW` 等一律不动)。
- **保守**:不是 INSERT、`INSERT ... VALUES`、查询顶层已有 `LIMIT` 时**一律不改**
  (宁可慢,也不能提交一条被改坏的 SQL)。
- **只作用于质量校验链路**。语法校验链路(`validate_deliverables`)要验证 SQL 的
  **真实行为**,加 LIMIT 会掩盖数据量/性能问题 —— 它保持原样。
- `sample_size <= 0`(不采样,全量扫描)时**不截断** —— 此时校验读的就是全量数据。

要放开/收紧截断量,调 `tools.yaml` 里 `trial_compute_table.config.quality_check.sample_size`
(截断量自动跟随它,两者必须一致,否则「采样量 > 写入量」会让校验看到的样本不完整)。

## 校验项

**一个校验项 = 一条 SQL 语句**,逐条执行;每条 SQL 内部用条件聚合一次算完
它负责的所有字段(不做「字段 × 校验项」的 UNION ALL 展开)。

**四个校验项,两种作用域**:

| 校验项 | 作用域 | 采样 | 说明 |
|---|---|---|---|
| `null_rate` | 字段级 | 是 | 逐字段 |
| `single_value_ratio` | 字段级 | 是 | 逐字段 |
| `outlier_ratio` | 字段级 | 是 | 逐字段(仅比率类) |
| `row_count` | **表级** | **否** | 整表行数,不挂字段 |

**字段级三项都基于同一批采样行** `(SELECT ... LIMIT sample_size) AS _src`,
`COUNT(1)` 只是这批采样行里的分母。

**表级一项(`row_count`)是全量 COUNT** —— 行数不是比率,采样算不出真实行数
(前 10000 行永远是 10000),所以它**不采样**,是本链路里唯一一条全表扫描的语句。
它**不挂在字段上**,结果写进 `table_metrics`,单独一张表展示。

### SQL 形状约束(生产 adHoc 踩过的坑)

改这个模块的 SQL 生成时,**必须**满足下面三条:

1. **不能用 CTE**(`WITH _src AS (...)`)。生产 adHoc 的表名解析阶段会把
   `FROM _src` 当成**物理表名**,直接报
   `table(s) agapdw._src do not exist in production environment`。
   采样源一律用**派生表**:`FROM (SELECT ... LIMIT N) AS _src`。
2. **`FROM` 后面不能出现裸别名**。同理,别名只能跟在 `AS` 后面;
   `FROM _src` 会被当成物理表,只有 `FROM (...) AS _src` 才安全。
3. **`LIMIT` 必须落在派生表内部**。`SELECT COUNT(1) FROM t WHERE ... LIMIT N`
   的 `LIMIT` 作用在**聚合结果**上(只有 1 行),根本限制不了扫描量 ——
   采样是假的。只有放进派生表才真正只取前 N 行。
   (例外:`row_count` 本来就要数全量,**不能**加 LIMIT。)

### 交付物自带 CTE 时的处理(提交前静态展开)

上面三条约束是**校验链路自己生成的 SQL** 的约束。交付物本身**可以带 CTE**
(`WITH _src AS (...) INSERT ...`),而质量校验链路会**解析**这份交付物 SQL ——
不处理 CTE 会出两个错:

1. **CTE 别名被当成物理源表** → 回源比对去查 `_src` → 必然 0 行 →
   全部判成「无法验证」,而且还会浪费一次 semantic schema 查询;
2. **回源 SQL 带上 CTE** → 生产 adHoc **不支持** `WITH AS` → 提交被拒 →
   也是「无法验证」。

处理方式是 **`dataops_validate_tool.expand_ctes_static`** —— 用 **sqlglot 静态**把
`WITH ... AS (...)` 内联成派生表。**为什么不用语法校验链路那套 LLM 内联**
(`_expand_cte_for_dml`):

- LLM 内联是**启发式**的 —— 可能改错、可能超时(上限 300s)、可能把口径改花;
- 质量校验只需要「**结构上**摊平 CTE」,不需要理解语义;
- sqlglot AST 展开是**确定性**的、**零延迟**的、**不改口径**的。

> ⚠️ **展开只发生在「提交给 DataOps 的那一步」** —— 落盘的交付文件、
> `source_sql`(自修正的增量修改底稿)、回源比对的 prompt 拿到的都是
> **未展开**的交付物原貌。
>
> **为什么不能提前展开**:展开后的 SQL 排版被重排(`PARTITION(pt_d=...)`
> 少空格、多行折成一行)。`source_sql` 是自修正改 SQL 的**底稿** —— 让它
> 看到展开版,等于让自修正在**重排过的** SQL 上改,改出来的东西与交付物
> 原貌脱节。

三处配套:

| 位置 | 作用 |
|---|---|
| `dataops_validate_tool.expand_ctes_static` | 静态摊平 CTE(提交前 + 回源 SQL) |
| `dataops_quality_check._execute_check_sql_prod` / `_execute_check_sql_integration` | **提交前**摊平 —— 只改提交给 DataOps 的那份 |
| `dataops_quality_check.extract_insert_source_tables` | 排除 CTE 别名(解析层看到的是原文) |
| `dataops_source_reconciliation` | 回源 SQL 摊平后再执行;prompt 明确禁止 CTE |

**展开失败时保持原 SQL** —— `expand_ctes_static` 保证不产出半成品 SQL(解析失败、
循环引用、非 SELECT 主体都会原样返回)。提交层展开失败就**带着 CTE 提交**
(保持原样),报错信息照样能看出是 CTE 问题。

**静态展开用 hive 方言** —— 生产 SQL 常见反引号包裹的 CTE 名
(``WITH `t` AS (...)``),**默认方言解析不了**(报 "Expecting (")。

> ⚠️ CTE 展开的**实现归属**在 `dataops_validate_tool.py`(两条链路共用一份,
> 不要另建模块)。但**两种展开方式各管一段**:
>
> | 函数 | 方式 | 用在哪 |
> |---|---|---|
> | `expand_ctes_static` | **sqlglot AST** | 质量校验链路**提交 SQL** 前 |
> | `_expand_cte_for_dml` | **LLM 内联** | 语法校验链路**提交 DML** 前 |
>
> 语法校验链路**没有**换成静态版 —— 那条链路展开的是**要提交给 DataOps 的
> DML**,涉及 `INSERT` 重写与临时表路由,LLM 内联在那里已经稳定运行,不在本次
> 改动范围内。两者可以叠加 —— LLM 内联失败时静态展开还能兜底。

### 空值覆盖率 `null_rate`

- **含义**: 字段非空比例,`非空率 = SUM(IF(字段有值, 1, 0)) / COUNT(1)`
- **判空写法**: 按**存储类型**区分 —— STRING 存储用 `!bicoredata.IsEmpty(col)`
  (空字符串也算空);数值/时间戳等非字符串存储用 `col IS NOT NULL`。
  类型从交付的 `create_*.sql` 里读;读不到就退回 `IS NOT NULL`。
- **默认阈值**: 非空率 ≥ 0.80(即空值率 ≤ 0.20),低于则 warning
- **告警文案**: `字段 'x' 非空率 0.4500 < 阈值 0.80(空值率 0.5500)`

### 单一值覆盖率 `single_value_ratio`

- **含义**: 出现次数最多的值占样本的比例,防止"全表一个值"
  `单一值比率 = max(各值出现次数) / 总样本数`
- **默认阈值**: ≤ 0.95,高于则 warning
- **告警文案**: `字段 'x' 单一值比率 0.9800 > 阈值 0.95`

### 异常值占比 `outlier_ratio`(仅比率类字段)

- **含义**: 比率类字段中取值越界(`< 0` 或 `> 1`)的样本占比
    `异常值占比 = SUM(IF(比率 < 0 OR 比率 > 1, 1, 0)) / COUNT(1)`
- **默认阈值**: 参考值 0 —— 出现一个越界取值即 warning
- **适用范围**: 只对**比率类字段**生效。比率类的判定:
  1. 配置了 `ratio_fields` → 以显式清单为准;
  2. 未配置 → 字段名(小写)包含任一 `ratio_field_patterns`(默认 `ratio`)模式。
     例如 `app_dl_retention_ratio_1d_udid`、`app_class_dl_retention_ratio_7d_udid`
     会被识别为比率类;`pay_cnt_udid` 这类非比率字段不出现在该校验项的 SQL 里,不参与告警。
- **告警文案**: `字段 'x' 异常值占比 0.0500 > 阈值 0.00(存在 <0 或 >1 的比率取值)`

### 数据行数 `row_count`(表级指标)

- **含义**: 校验对象(临时表或真实目标表)在这个分区里到底有多少行
  `SELECT COUNT(1) FROM <表> WHERE <分区条件>`
- **不采样**: 行数不是比率 —— 采样算出来的「前 N 行」永远是 N,没有意义。
  这条是**全量 COUNT**,是本链路唯一一条全表扫描的语句。
- **不挂字段**: 结果写进 `table_metrics`,**不**进逐字段的 `details`,
  也**不**进那张比率表 —— 它单独一张表(`| 表级指标 | 值 |`)。
- **默认阈值**: 行数 ≥ 1(下限)。行数太少通常是 JOIN 写错(INNER JOIN 丢数据)、
  分区过滤条件写错、或上游数据没落盘。
- **告警文案**: `数据行数 0 < 阈值 1(疑似 JOIN 丢数据 / 分区过滤条件写错 / 上游数据未落盘)`
- **试算链路的注意事项**: 试算 INSERT 被截断到 `sample_size` 行,所以临时表的
  行数只是**下界**(真实数据可能更多)。结果里带 `row_count_truncated: true`,
  表格里会标注「≥，试算写入被截断」。直连真实目标表时不受这个影响。
- **关掉它**: 从 `checks` 里去掉 `"row_count"`,或把 `row_count_threshold` 调低。

### 默认排除字段

分区列(`pt_d`/`pt`/`pt_h`)、时间戳(`create_time`/`update_time`/`modify_time`)、
主键 ID(`id`/`uuid`/`udid`/`guid`/`row_key`)—— 这些字段做覆盖率统计没有业务含义。

## 数据正确性四规则(新增)

前三个校验项回答「字段填得满不满」;**正确性四规则**回答的是「数据对不对」——
填满了不代表口径没错。它们分成两类:

- **确定性规则**(`pk_duplicate_rate` / `negative_count`):一条 SQL + 一个阈值,
  走 `QualityChecker` 那套,与已有校验项共用编排层;
- **流程型规则**(`common_sense` / `source_reconciliation`):要多步骤 ——
  抽样 → 模型判定 / 生成回源 SQL → 执行 → 比对,走 `FlowChecker` 那套。

两类结果合并成统一的 `violations` 列表,和 `warnings` 一起进报告。

### 主键重复率 `pk_duplicate_rate`(表级,必须为 0)

- **含义**: `重复率 = 重复行数 / 有效行数` —— 主键为 NULL 的行从分母里剔除
  (NULL 不算重复)。
- **默认阈值**: **0** —— 出现任何重复即 error 级违规,触发自修正。
- **主键来源**(按优先级,**只认 TASKIR 与显式配置**):
  1. `DataTaskIR.identity.components` —— 明细/特征任务的逻辑主键;
  2. `DataTaskIR.output_features[].aggregation.grain` —— **聚合表**(DM 汇总表)
     的记录粒度。IR 模板明确写「若某列既是最终记录主键又是聚合分组键,
     只在 identity 声明;`aggregation.grain` 可以**引用**同一 field_ref」,
     所以这两处是同一套口径的两个视角;
  3. `quality_check.primary_key_columns` 配置 —— 非 IR 场景显式指定;
  4. 都拿不到 → **跳过**该项并在报告的「已跳过的校验项」里注明「未提供主键列」,
     **不猜**主键(宁可漏查,也不能用错的主键给出假结论)。
- **候选集按特异性降序尝试**(每组都必须「全有或全无」匹配上目标表的列):
  1. `identity.components ∪ grain` —— **并集最完整**,两处描述的是同一张表的
     粒度。这条同时修掉「identity 只填了 1 列、真实粒度是 2 列」的漏填。
  2. `grain` 单独;3. `identity.components` 单独;4. 显式配置。
  某一组对不上就**整组作废**并试下一组,绝不降级成子集。
- **单一主键还是复合主键,由声明本身决定** —— `identity.components` 与
  `grain` 都是**列表**,声明几列就是几列,整组一起用(见下条)。
- **绝不从 INSERT 的 `GROUP BY` 推导主键** —— `GROUP BY` 是 SQL 的**实现细节**,
  不是业务主键的权威声明:它可能多带分区列、可能按月累加导致本就不唯一、
  也可能与业务主键粒度不同。主键只认 TASKIR 的口径声明。
- **绝不从排除字段列表反推主键** —— 那份列表是按「无业务含义」筛的,
  与「主键」不是一回事,反推会漏掉复合主键成员,把重复率低估。
- **主键声明必须「全有或全无」地匹配上目标表的列** —— 这是复合主键场景
  最容易踩的坑。主键声明是 `(a, b)` 复合主键、但目标表里只有 `a` 时,
  如果丢掉对不上的成员、只拿 `a` 去算,就把复合主键**静默降级成单列主键**。
  而单列 `a` 在复合粒度的表里天然有大量重复,会报出**误报**。
  生产实测就是这个坑:IR 声明了
  `(app_second_class_cn_name, conversionbehaviorid)` 两列主键,匹配上 1 列、
  丢掉 1 列 → 262 行里 227 行被判成「重复」(重复率 0.866412),
  而真实重复率是 0。这个误报还会**淹没真正的问题**。
  所以对不上就**整组作废** → 跳过该项并在报告里注明原因。
- **全表算,不采样**: 这是需求明确要求的 —— 采样算不出真实重复率。
  注意试算链路的临时表本身只有 N 行(写入被截断),所以试算链路算的是
  「临时表这 N 行里的重复率」,报告会标注口径;直连真实目标表时是**真实全表**指标。
- **复合主键: 直接多列 `GROUP BY`,不做字符串拼接** —— 早先这里用
  `CONCAT_WS('\x01', a, b)` 拼成单值再分组,那在**生产 adHoc** 上被直接拒绝,
  实测报错:
  ```
  org.apache.spark.sql.AnalysisException: Undefined function: 'CONCAT_WS'.
  This function is neither a registered temporary function nor a permanent
  function registered in the database 'agapdw'.
  ```
  **根因**:生产 adHoc 跑的是 **Spark SQL**(报错栈是
  `SparkExecuteStatementOperation`),`CONCAT_WS` 是 **Hive 函数**,Spark
  没注册它。**而且这个拼接本来就没必要** —— 内层的 `pk` 列**根本没被用到**
  (外层只聚合 `cnt`),多列 `GROUP BY` 语义完全等价,却只碰
  `GROUP BY`/`COUNT`/`SUM` 这些最基本的构造。
  → **只允许用 Spark SQL 一定支持的函数**。`IF()` / `SUM` / `COUNT` /
  `MAX` / `bicoredata.IsEmpty`(平台 UDF)都行;`CONCAT_WS` / `NVL` /
  `SPLIT` / `FROM_UNIXTIME` 这类 Hive-only 的不行。
- **SQL 必须用 `GROUP BY` 派生表,不能用 `COUNT(DISTINCT ...)`** ——
  生产 adHoc **硬性拦截** `COUNT(DISTINCT)`,实测报错:
  ```
  AdHoc stage for parsing sql:ERROR:Do not display count(distinct) in your hive sql,
  because there is a performance problem.It is recommended that you use group by.;
  ```
  形状:
  ```sql
  SELECT
    CASE WHEN SUM(cnt) = 0 THEN 0
      ELSE SUM(CASE WHEN cnt > 1 THEN cnt - 1 ELSE 0 END) / SUM(cnt) END AS pk_duplicate_rate,
    SUM(CASE WHEN cnt > 1 THEN cnt - 1 ELSE 0 END) AS pk_duplicate_rows,
    SUM(cnt) AS pk_total_rows
  FROM (
    SELECT COUNT(1) AS cnt
    FROM <表> WHERE <分区> AND <主键非空>
    GROUP BY <主键列1>, <主键列2>, ...
  ) AS _pk_groups
  ```
- **总行数用 `SUM(cnt)`,不能用外层 `COUNT(1)`** —— 外层 `COUNT(1)` 数的是
  **分组数**(唯一主键数),不是总行数,用错会让重复率整体算错。
- **除零保护用 `CASE WHEN SUM(cnt) = 0`**,**不用** `CAST(... AS DOUBLE)` /
  `NULLIF(...)` —— 这两个函数在生产 adHoc 的表名解析与引擎路由阶段会被拒。
- **用派生表而不是 CTE** —— 生产 adHoc 的表名解析阶段会把 `FROM _src` 当成
  物理表名去解析并报「表不存在」。
- **报告里永远有一行「主键重复率」**: 该 checker 失败时指标值渲染成「未完成」,
  **不能整项消失**(消失会被误读成「没这项校验」)。同时输出
  「主键重复行数」「主键有效行数」两行 —— 用户要看到「重复了多少行」。

### 计数型字段非负 `negative_count`(字段级,必须 ≥ 0)

- **含义**: `负值率 = SUM(IF(字段 < 0, 1, 0)) / COUNT(1)`
- **默认阈值**: **0** —— 出现任何负值即 error 级违规。
- **哪些字段算「计数型」**(按优先级):
  1. `quality_check.count_fields` 显式清单 → 以清单为准,清单外字段一律不算;
  2. 未配置 → 字段名(小写)包含任一 `count_field_patterns`
     (默认 `cnt` / `count` / `_num` / `times` / `_times`)**且** DDL 类型是数值型;
  3. 类型读不到 → **跳过**并注明「类型未知」,不猜。
- **差值型排除**: 字段名匹配 `negative_exclude_patterns`
  (默认 `diff` / `delta` / `change` / `chg`)的字段**不参与**本校验 ——
  差值天然可正可负,判负会产生大量误报。

### 常识违规 `common_sense`(流程型,抽样 + 模型判定)

- **抽样**: 从目标表抽 `common_sense_sample_size`(默认 20)行**完整数据**。
- **判定**: 交给模型看**同一行上多个字段的联合语义** —— 年龄为 -5 单看
  「年龄是数值」没问题,但「VIP 与新用户同时为真」只有把字段放在一起才看得出来。
  这类判定没法写成 SQL 谓词。
- **提示词输入**: 抽样行 + 每字段的名称 / 类型 / DDL COMMENT + 目标表名 + 分区条件。
- **只接受带具体违反依据的输出** —— 模型给不出依据的 MUST NOT 报告。否则
  「不确定」会被当成「违规」,自修正会去「修」本来正确的 SQL。
- **数值精度差异 MUST NOT 判成违规** —— 模型的 `expected` 是**手算四舍五入后**
  的值,`observed_value` 是原始精度。真实案例:`target_conversion_rate`
  期望 `939 / 3327 * 1.2 ≈ 0.3387`,实测 `0.338683` —— 相对差 `5e-5`,
  只是**显示精度**不同,不是口径错误。
  - 判定容差:相对 `1e-3`(0.1%)/ 绝对 `5e-4` —— 与回源比对的 `1e-6`/`1e-9`
    **刻意不同**:回源比对两侧都来自 SQL 查询(原始精度),常识判定的
    `expected` 是模型手算的(带显示精度)。
  - 模型自认「不视为违规 / 基本一致 / 精度差异 / 四舍五入」时,
    **以它为准** —— 模型常自相矛盾(`reason` 说「违反公式规则」,`expected`
    说「不视为违规」)。
  - **误报比漏报更糟** —— 误报会以 `error` 级触发自修正,去「修」一份本来
    正确的 SQL。
- **判定与修正职责分离**: 模型只做判定,**不产出修正 SQL**。修复是自修正
  闭环里另一次独立的、带完整上下文的调用。混在一次调用里,模型会因为
  「要改」而放宽标准,误报率显著上升。
- **降级**: 模型不可用 / 返回不是合法 JSON → 本项记「未完成」,不影响其它
  校验项,也不让整轮校验失败。

### 抽样回源比对 `source_reconciliation`(流程型,抽样 3 行回源)

流程严格按需求原文的顺序:

```
INSERT 落地的表 ──► 抽 3 行(含主键取值与全部校验字段取值)
                     │
                     ▼
    模型读这 3 行的实际含义 + 源表 schema + 原始 INSERT SQL
                     │
                     ▼
    为每行生成 1 条只读 SELECT(回源表取数,别名 = 特征表列名)
                     │
                     ▼
    执行这 3 条 SQL(并发) → 逐行逐字段比对
```

- **为什么必须回源**: 特征表里的数字本身就是待验证对象。只查特征表只能验证
  「内部自洽」(重复、非负),验证不了「口径对不对」。回源是唯一能发现
  「数字算错了但看起来合理」的手段。
- **为什么由模型生成 SQL 而不是模板拼接**: 源表可能有不同的分区口径、不同的
  JOIN 结构、需要先聚合再关联 —— 模板拼接覆盖不了。
- **只读白名单是硬闸门**: 生成结果会被**真的执行**,所以只读性 MUST 用程序
  卡死(只能 `SELECT`/`WITH`、不得含写操作/DDL 关键字、只能一条语句),
  不能靠提示词约束。不合规的直接丢弃并记「无法验证」,**绝不执行**。
- **必须带分区过滤**: 生产 adHoc 不带分区会被拒;生成结果里没有分区条件
  (形如 `pt_d = '...'`)的同样丢弃。
- **不能用 CTE(独立于只读白名单的一道闸门)**: 只读白名单允许 `WITH` 开头
  (它只管「不改数据」),但生产 adHoc **不支持** `WITH AS` —— 会把
  `FROM _src` 当物理表名并报「表不存在」。所以回源 SQL 必须先经
  `expand_ctes_static` **静态摊平**再执行;摊平后仍带 CTE 的直接丢弃并记
  「无法验证」,**绝不**提交一条引擎读不懂的 SQL。prompt 里也明确禁止 CTE。
- **比对口径**: 数值按 `ABS(差) <= max(绝对容差, 相对容差 × |源表值|)`
  (默认相对 1e-6、绝对 1e-9);字符串精确比较;两侧都 NULL 才算相等。
- **源表返回 0 行 ≠ 不匹配**: 源表过滤条件比特征任务更严格时这很常见,
  判成不匹配会产生大量误报 —— 误报会让自修正去「修」本来正确的 SQL。
  这种情况记「无法验证」。
- **「比过了」≠「通过了」(假阳性防护,生产实测踩过)**: 源表返回的列名与特征
  字段名**完全对不上**时(模型没按特征字段名起别名),比对逻辑会一个字段都
  比不上 —— 这种情况 MUST 记「无法验证」(并写明期望列与实际列),
  **绝不**算「成功比对 / 通过」。否则口径完全错误的 SQL 会顺利交付。
- **报告里 MUST 同时给出 `matched_rows` 与 `compared_fields`**: 前者只说明
  「这一行的回源查询成功且列能对齐」,后者才说明「真的拿几个字段值做了
  比对」。只给「成功比对 3 行」会被读成「3 行 × 全部字段都核对过」。
- **必须摊开查询证据**: 报告的「回源比对查询证据」小节要列出
  **真正执行过的回源 SQL**、每行的**源表实际取值 vs 特征表取值**、
  以及**无法验证的行及原因**。回源比对跑在「模型生成 SQL → 执行 → 比对」的
  链路上,中间任何一环降级都可能被读成「通过」—— 用户要能自己判断
  「这到底算不算真的回源核对了」。

### 自修正闭环(校验出错后自动修正)

**只在出现 error 级违规时触发** —— 仅 warning 级时跳过修正。

```
error 级违规
    │
    ▼
构造修正上下文(当前 INSERT SQL + 每条违规的 rule/field/evidence/suggestion)
    │
    ▼
模型产出**增量补丁**(old_text → new_text),在原 SQL 基础上修改
    │
    ▼
备份原文件(insert_<table>.sql.bak.<round>)→ 写回修正后的 SQL
    │
    ▼
重新试算 + **重走整个校验流程**(全部确定性校验项 + 常识违规 + 回源比对)
    │
    ├─ 违规清空 **且复检完整** → 收敛,报告「自修正成功(第 N 轮)」
    ├─ 仍有违规且轮次 < 上限 → 继续下一轮
    ├─ 复检**本身没跑完** → 本轮不算收敛(见下)
    └─ 轮次用尽 → needs_manual_review=true,报告写明「需人工复核」
```

**每一轮自修正后都必须重走整个流程做数据校验** —— 不是只重跑那一条
触发修正的校验项。原因:改了 SQL 之后,其它校验项的结论**全部失效**
(比如改了分母,重复率、比率类指标、常识违规的判定全都变了)。
只重跑一条等于用旧结论证明新 SQL 是对的。

**复检不完整 ≠ 校验通过**。复检时如果某个 checker 超时、常识判定降级、
回源比对跑不了,违规列表可能恰好是空的 —— 但那是「没查出来」,不是
「查过且通过」。所以闭环会检查复检结论里的 `failed_checks` / `partial`:
复检不完整时**本轮不判定为收敛**,继续下一轮。

**必须是增量补丁,不是整条重写**。数据特征任务的 SQL 通常几十上百行,
里面有大量**本来就对**的部分 —— 过滤条件、JOIN 关系、时间窗口。整条重写等于
让模型凭几条违规信息重建整条 SQL,改坏的地方往往比修好的地方多。

补丁协议与硬约束:

```json
{"edits": [{"old_text": "原 SQL 片段", "new_text": "替换后的片段"}], "reason": "改了什么、为什么"}
```

- `old_text` MUST 逐字来自当前 SQL,且在当前 SQL 里**只出现一次** ——
  出现多次就无法确定改的是哪一处,整条补丁作废,本轮记为失败。
- `new_text` MUST 与 `old_text` 不同;多个补丁的锚点 MUST NOT 重叠。
- 一次调用只针对**一条**违规(一条违规一次模型调用),输出 1~3 条补丁。
- 下一轮的底稿是**本轮修正后**的 SQL —— 增量地继续改,不是从最初始重来。
- 修正不了就给空 `edits` 并说明原因 —— 不许瞎改。

**轮次预算**:`self_correct_max_rounds`(默认 3),与
`DEFAULT_DATAOPS_VALIDATION_ATTEMPTS`(DataOps 提交次数上限,=10)**互相独立**,
两者 MUST NOT 互相覆盖。

**收敛失败不许静默**: 报告必须显式写「需人工复核」,并列出未收敛的违规全文。
**必须写回 workspace**: 只修内存里的 SQL 没有意义,用户交付的是文件。
**必须先备份**: 修正会覆盖用户的 `insert_*.sql`,备份为
`insert_<table>.sql.bak.<round>`,保留原始交付物可追溯。

## 配置键速查

全部挂在 `tools.yaml` 的 `quality_check` 段下(`trial_compute_table` 与
`dataops_validate_data` 各有一份)。优先级:
`tool_config` > `dataops` resource metadata > 环境变量 > 默认值。

| 配置键 | 默认值 | 作用 |
|---|---|---|
| `checks` | `null_rate` / `single_value_ratio` / `outlier_ratio` / `row_count` | 生效的**质量指标**校验项 |
| `correctness_checks` | 四条正确性规则全开 | 生效的**正确性**校验项(独立于 `checks`);写 `[]` 全关 |
| `thresholds.pk_duplicate_rate_threshold` | `0.0` | 主键重复率上限(必须为 0) |
| `thresholds.negative_count_threshold` | `0.0` | 计数型字段负值占比上限(必须为 0) |
| `primary_key_columns` | 空 | 显式主键列;不写时从 `DataTaskIR.identity.components` / `output_features[].aggregation.grain` 取 |
| `count_fields` | 空 | 显式计数型字段清单;配置了以清单为准 |
| `count_field_patterns` | `cnt` / `count` / `_num` / `times` / `_times` | 计数型字段名模式(小写子串匹配) |
| `negative_exclude_patterns` | `diff` / `delta` / `change` / `chg` | 差值型字段名模式 —— 匹配到的**不参与**负值校验 |
| `common_sense_sample_size` | `20` | 常识检测抽样行数 |
| `reconcile_sample_size` | `3` | 回源比对抽样行数 |
| `relative_tolerance` | `1e-6` | 回源比对数值相对容差 |
| `absolute_tolerance` | `1e-9` | 回源比对数值绝对容差 |
| `self_correct_enabled` | `true` | 自修正开关;关掉后只输出违规,不调模型 |
| `self_correct_max_rounds` | `3` | 自修正轮次上限(与 DataOps 提交次数上限独立) |

## 什么时候用

| 场景 | 动作 |
|---|---|
| **用户输入「执行数据校验」** | 调 `dataops_validate_data()` —— 见 `dataops_validate_data` skill |
| 想在交付前自查 | 主动调 `trial_compute_table(target_table, dml_path="<workspace>/insert_*.sql")` |
| 只想校验已有结果 | 调 `trial_compute_table` 但 SQL 已在 workspace;试算与校验共用同一份逻辑 |

**两条链路的边界**（别用错）：

| | `trial_compute_table`（本 skill） | `dataops_validate_data`（独立 skill） |
|---|---|---|
| 建表 / 插入 | 建临时表 → 试算 INSERT | 建临时表 → 试算 INSERT（同一条试算链路） |
| 校验对象 | `adhoctemp.tmp_*` 临时表 | `adhoctemp.tmp_*` 临时表 |
| 自修正闭环 | **不做** | **做**（error 级违规 → 改 SQL → 重走全流程 → 复检） |
| 结论落盘 | 不落盘 | **落盘** `.runtime/data_quality_<hash>.json` |
| 适用 | 「这条 SQL 跑不跑得通 + 数据对不对」 | 「完整的数据校验闭环」 |

两条链路共用同一套校验器（`dataops_quality_check._BUILTIN_CHECKERS`）和同一个
Markdown 表格渲染器（`render_metrics_table`），所以产出的表格形状完全一致。

## 返回值解读

```json
{
  "passed": true,
  "summary": "[trial_compute] PASSED job_id=xxx | quality_check=2 warning(s)",
  "quality_check": {
    "ok": false,
    "warnings": ["字段 'x' 非空率 0.45 < 阈值 0.80(空值率 0.55)"],
    "violations": [
      {
        "rule": "pk_duplicate_rate", "field": "udid", "severity": "error",
        "message": "主键重复率 0.0100 > 阈值 0.00",
        "evidence": {"duplicate_rate": 0.01},
        "suggestion": "检查 GROUP BY 是否漏了主键列"
      }
    ],
    "details": { "x": {"null_rate": 0.45, "single_value_ratio": 0.12, "outlier_ratio": null} },
    "table_metrics": { "row_count": 10000, "pk_duplicate_rate": 0.0,
                       "pk_duplicate_rows": 0, "pk_total_rows": 10000 },
    "sampled": true, "sample_size": 10000, "job_id": "...",
    "checks": ["null_rate", "single_value_ratio", "outlier_ratio", "row_count"],
    "correctness_checks": ["pk_duplicate_rate", "negative_count"],
    "skipped_checks": [{"name": "source_reconciliation", "reason": "未提供主键列"}],
    "flow_notes": {
      "common_sense": {"status": "completed", "sample_rows": 20, "violation_count": 0},
      "source_reconciliation": {
        "status": "completed", "sample_rows": 3,
        "matched_rows": 3, "compared_fields": 9,
        "executed_sql": [{"row_index": 1, "sql": "SELECT ...", "reason": "口径说明"}],
        "comparisons": [{"row_index": 1, "primary_key": {"udid": "u1"},
                         "compared_fields": 3, "source_row": {...}, "feature_row": {...}}],
        "unverifiable": []
      }
    },
    "self_correct": {
      "status": "converged", "converged": true, "rounds": 1,
      "needs_manual_review": false, "backups": ["insert_x.sql.bak.1"],
      "diff_summary": "- GROUP BY udid\n+ GROUP BY udid, dt"
    },
    "row_count_truncated": true, "row_limit": 10000,
    "temp_table": "adhoctemp.tmp_xxx_20261015_ads_yyy", "partition_date": "20261015"
  }
}
```

- `details` —— **字段级**指标(三个比率校验项)
- `table_metrics` —— **表级**指标(`row_count` / `pk_duplicate_rate` /
  `pk_duplicate_rows` / `pk_total_rows`),不挂字段。表级 checker 的 key
  **预填**—— 失败时值是 `null`(报告里渲染成「未完成」),**不会整项消失**。
- `violations` —— **结构化违规**列表(正确性四规则 + 已有校验项),每条带
  `rule`/`field`/`severity`/`message`/`evidence`/`suggestion`。`warnings` 由它派生。
- `skipped_checks` —— 因**信息不足**而跳过的校验项(不是失败)。例如没提供主键列时
  主键重复率与回源比对都会被跳过 —— 这时 MUST NOT 说成「查过了、通过」。
- `flow_notes` —— **流程型校验**(常识检测 / 回源比对)的旁路记录:
  `{checker_name: {status, reason, sample_rows, violation_count, matched_rows,
  compared_fields, missing_fields, executed_sql, comparisons, unverifiable}}`。
  这两条规则的结果不在 `details` 里,必须靠它才能知道「跑没跑、抽样了几行、
  比对了几个字段、哪些行无法验证」。
- `failed_checks` —— 没能产出结果的校验项,每项带 `reason`。
  `reason` 是 **DataOps 的真实报错**(如「表不存在」「语法被拒」),
  不是干巴巴的 `status='error'` —— 排查时直接看它。
- `self_correct` —— 自修正闭环结果;`needs_manual_review=true` 时**不要**直接交付。
- `row_count_truncated` —— 为 `true` 时行数只是**下界**(试算写入被截断过)

| 字段 | 你的下一步 |
|---|---|
| `passed: true` + `quality_check.ok: false` | 试算跑通了,**数据有问题**。看 `warnings` 逐字段修 SQL(补 JOIN、去掉 INNER JOIN 丢数据、检查口径),改完重写 `insert_*.sql` 再交付 |
| `passed: false` | SQL 跑不起来。读 `log_analysis`(error_type/location/suggestions)修 SQL |
| `skipped: true` | 看 `reason`(`no_dml_path`/`empty_sql`),自己补齐入参 |
| `quality_check` 缺失 | 校验被跳过:不是 INSERT、没解析出可校验字段、或无 dataops resource |
| `self_correct.needs_manual_review: true` | **自修正没收敛**。读 `self_correct.remaining_violations` 与 `diff_summary`,人工改 `insert_*.sql` 后再交付 —— 不要直接按当前 SQL 交付 |

## 关键约束(Must Follow)

1. **试算不改真实表**。INSERT 目标被改写成 `adhoctemp.tmp_<user>_<date>_<table>`,
   校验完立即 DROP(不管成功与否)。真实目标表不受影响。
2. **质量校验前先重建临时表**。语法校验链路已经把它自己那张临时表删了,
   而且两条链路日期不同(一年之后 vs 一周之前),根本不是同一张表。
   质量校验链路必须按交付物里的 `create_*.sql` 再建一次,才能往里写数据。
3. **`passed` 不因数据质量翻假**。试算失败(`passed=false`)和数据有问题
   (`quality_check.ok=false`)是两件事,前者是 SQL 跑不通,后者是 SQL 能跑但数据不对。
4. **必须在临时表 DROP 之前校验**。`run_quality_check` 依赖临时表存在;
   试算链路保证了这一顺序(先校验、后 DROP)。
5. **采样而非全表**。默认 `LIMIT 10000` 行,不代表全量分布;极端倾斜数据请调大
   `tools.yaml` 里 `trial_compute_table.config.quality_check.sample_size`。
   调大采样量的同时,试算的写入量上限也会跟着调大(见上文「试算写入量会被截断」)。
6. **DDL 不做数据校验**。`CREATE TABLE` 没有数据可查,只试算执行。
7. **校验结论 MUST 落盘**。`dataops_validate_data` 会把完整结论写到
   `<workspace>/.runtime/data_quality_<hash>.json`(内容哈希命名 ——
   同一份结论不会重复落盘,内容变了就是新文件,能看出改了几版)。
   没有落盘产物的话,事后无法复核「当时查了什么、结论是什么」。
8. **报告 MUST 有显式的「交付结论」段**。质量校验跑在**交付之后**,所以
   SQL checker 的「✅ PASS」与质量校验的「发现 N 处告警」在时间上并存。
   报告必须明确写清:① 校验项**是否全部跑完**(部分完成 ≠ 通过);
   ② 是否还有**未解决的 error 级违规**;③ 结论是「可交付」还是
   「需要人工复核后再交付」。不许让读者自己去猜该信哪个。

## 扩展:新增第 N 种校验

在 `actions/tools/local_tool/dataops_quality_check.py` 的 `_BUILTIN_CHECKERS`
追加一个 `QualityChecker`,并在 `tools.yaml` 的
`trial_compute_table.config.quality_check.checks` 里按名称启用。**编排层无需改动**。

一个 checker 需要描述四件事:

| 字段 | 作用 |
|---|---|
| `build_sql` | 生成这个 checker 的**完整 SQL 语句**(一次算完它负责的所有字段) |
| `violates` | `(实测值, 阈值) -> 是否违规`(支持大于/小于两种方向) |
| `format_warning` | 生成人类可读 warning |
| `applies_to` | (可选)`(字段名, 配置) -> 是否适用于该字段`;不适用字段不出现在该 checker 的 SQL 里,也不告警。用于"只对某一类字段生效"的校验 |
| `scope` | (可选)`"field"`(默认)或 `"table"`。表级指标(如数据行数)不挂字段,结果写进 `table_metrics` |

**执行规则(已在编排层实现,无需关心):一个 checker 一条 SQL,并发提交。**
不会把「checker × 字段」拼成一条 `UNION ALL` 大查询 —— 那种 SQL 又长又难读,
还把临时表反复扫很多遍。

**为什么是并发而不是顺序**:DataOps 不支持把多个校验项合并成一条语句。
顺序跑的话,第一条挂掉后面的全不跑,整轮校验直接被判成「未执行」。
并发提交(`asyncio.gather`)后各条各自独立轮询,互不影响 —— 一条失败
仍会如实报 `sampled=false`,但其它条的结果不会白白丢掉。

**新增一条正确性规则**时:能写成「一条 SQL + 一个阈值」的,注册进
`_CORRECTNESS_CHECKERS`(与 `_BUILTIN_CHECKERS` 并列,保持历史 `details`
形状不变);需要多步骤流程的(抽样 → 模型 → 执行 → 比对),实现成
`FlowChecker` 并加进 `dataops_flow_checkers.build_flow_checkers()`。

配置优先级:`tool_config` (tools.yaml) > `dataops` resource metadata > 环境变量 > 默认值。

## 思考模式开关

校验链路里的 LLM 调用(CTE 内联、失败日志分析)走 per-call kwargs,
**默认关闭思考模式** —— 这些都是「短提示 + 结构化输出」的任务,思考链只
拉长响应、放大 token 消耗,还可能把 `<think>` 片段混进 JSON 正文。

开关**按工具配置**控制(不做全局控制),在 `tools.yaml` 的对应条目里加
`config.enable_thinking`:

```yaml
- name: "trial_compute_table"
  config:
    enable_thinking: false    # 默认就是关闭的
```

改一个工具的配置不会影响其它链路,更不会影响主 Agent 的 planner。

## 相关文档

- 试算/探查工具: `dataops_probe` skill(字段存在性/取值范围探查,本 skill 不覆盖)
- 独立数据验证: `dataops_validate_data` skill(只查真实目标表,不建表不插入)
- SQL 规范校验: `validate_deliverables` 工具(语法/GATE/8 类质量自检,本 skill 不覆盖)