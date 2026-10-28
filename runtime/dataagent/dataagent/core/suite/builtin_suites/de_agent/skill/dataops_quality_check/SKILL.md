---
name: dataops_quality_check
description: |
  Use when (任一即触发):
    1. 数据试算 —— 需要把写好的 INSERT 真正跑一遍看看产出对不对;
    2. 交付后自动执行 —— de_agent 在交付 SQL 后会对 insert_*.sql 自动跑一次
       试算 + 数据正确性校验,不需要你主动调用;
    3. 单独调用数据验证 —— 只想对已有临时表/结果做数据正确性校验,不跑试算。
  能力:空值覆盖率(non-null rate) + 单一值覆盖率(single-value ratio)
    + 异常值占比(outlier ratio,仅比率类字段)。
    三者都是**可扩展校验器**里的条目,新增第四种校验只需注册一个 QualityChecker。
  实现:`actions/tools/local_tool/dataops_quality_check.py`(校验器注册表 +
    SQL 生成 + 结果解析),试算工具 `de_agent/tools/dataops_trial_compute_tool.py`。
  NOT for: 字段是否存在/取值范围探查(那是 dataops_probe 的活);
    SQL 语法/规范校验(那是 validate_deliverables 的活);
    **只想对真实目标表做体检、不建表不插入**(那是 dataops_validate_data 的活)。
---

# 数据正确性校验 (Data Quality Check)

## 核心心智模型

一条链路,一个触发点,一套校验器:

```
  ①交付后 post-hook                      ②主动调用
  hooks.dataops_quality_check_post        trial_compute_table
        └─ trial_compute_table                  └─ 重建临时表(按 create_*.sql)
               └─ 重建临时表                            │
                      └─ 试算到临时表                     └─ 试算 → 校验 → DROP
                              └─ run_quality_check() ← 同一个函数 ─────┘
```

校验器注册表 `_BUILTIN_CHECKERS`（可扩展）：`null_rate`（空值覆盖率）、
`single_value_ratio`（单一值覆盖率）、`outlier_ratio`（异常值占比，仅比率类字段）。

**静态校验链路(`validate_deliverables` → `dataops_validate_sql_with_log_analysis`)
不执行数据正确性校验** —— 那条链路要保持"快",好让 agent 快速迭代修 SQL;
昂贵的采样统计统一放到交付后由 post-hook 执行一次。

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

## 校验项

**一个校验项 = 一条 SQL 语句**,逐条执行;每条 SQL 内部用条件聚合一次算完
它负责的所有字段(不做「字段 × 校验项」的 UNION ALL 展开)。

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

### 默认排除字段

分区列(`pt_d`/`pt`/`pt_h`)、时间戳(`create_time`/`update_time`/`modify_time`)、
主键 ID(`id`/`uuid`/`udid`/`guid`/`row_key`)—— 这些字段做覆盖率统计没有业务含义。

## 什么时候用

| 场景 | 动作 |
|---|---|
| 交付后(默认) | **什么都不用做**,hook 自动跑 `trial_compute_table` |
| 想在交付前自查 | 主动调 `trial_compute_table(target_table, dml_path="<workspace>/insert_*.sql")` |
| 只想校验已有结果 | 调 `trial_compute_table` 但 SQL 已在 workspace;试算与校验共用同一份逻辑 |
| **只想对真实目标表做体检**（不建表、不插入、不试算） | 调 `dataops_validate_data(sql=...)` —— 见 `dataops_validate_data` skill |

**两条链路的边界**（别用错）：

| | `trial_compute_table`（本 skill） | `dataops_validate_data`（独立 skill） |
|---|---|---|
| 建表 / 插入 | 建临时表 → 试算 INSERT | **不建、不插** |
| 校验对象 | `adhoctemp.tmp_*` 临时表 | **真实目标表** `db.table` |
| 临时表替换 | 做 | **不做**（只读） |
| 适用 | 「这条 SQL 跑不跑得通 + 数据对不对」 | 「这张表现在的数据对不对」 |

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
    "details": { "x": {"null_rate": 0.45, "single_value_ratio": 0.12, "outlier_ratio": null} },
    "sampled": true, "sample_size": 10000, "job_id": "...",
    "checks": ["null_rate", "single_value_ratio", "outlier_ratio"],
    "temp_table": "adhoctemp.tmp_xxx_20261015_ads_yyy", "partition_date": "20261015"
  }
}
```

| 字段 | 你的下一步 |
|---|---|
| `passed: true` + `quality_check.ok: false` | 试算跑通了,**数据有问题**。看 `warnings` 逐字段修 SQL(补 JOIN、去掉 INNER JOIN 丢数据、检查口径),改完重写 `insert_*.sql` 再交付 |
| `passed: false` | SQL 跑不起来。读 `log_analysis`(error_type/location/suggestions)修 SQL |
| `skipped: true` | 看 `reason`(`no_dml_path`/`empty_sql`),自己补齐入参 |
| `quality_check` 缺失 | 校验被跳过:不是 INSERT、没解析出可校验字段、或无 dataops resource |

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
6. **DDL 不做数据校验**。`CREATE TABLE` 没有数据可查,只试算执行。

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

**执行规则(已在编排层实现,无需关心):一个 checker 一条 SQL,并发提交。**
不会把「checker × 字段」拼成一条 `UNION ALL` 大查询 —— 那种 SQL 又长又难读,
还把临时表反复扫很多遍。

**为什么是并发而不是顺序**:DataOps 不支持把多个校验项合并成一条语句。
顺序跑的话,第一条挂掉后面的全不跑,整轮校验直接被判成「未执行」。
并发提交(`asyncio.gather`)后三条各自独立轮询,互不影响 —— 一条失败
仍会如实报 `sampled=false`,但另外两条的结果不会白白丢掉。

配置优先级:`tool_config` (tools.yaml) > `dataops` resource metadata > 环境变量 > 默认值。

## 相关文档

- 试算/探查工具: `dataops_probe` skill(字段存在性/取值范围探查,本 skill 不覆盖)
- 独立数据验证: `dataops_validate_data` skill(只查真实目标表,不建表不插入)
- SQL 规范校验: `validate_deliverables` 工具(语法/GATE/8 类质量自检,本 skill 不覆盖)