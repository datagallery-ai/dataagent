# step2_0_source_data_analyze.md 输出格式模板

> ⛔ 以下是 step2_0_source_data_analyze.md 的唯一合法格式。禁止自由发挥。

---

## 文档结构要求

1. 源表概览 + 表分类（1:1 / 1:N / 游戏维度表）
2. **每张源表独立一节 `### <table_name>（N 列）`**，含 7 列表格（见下方模板）
3. **唯一允许的独立小节**：`## 列表字段检测结果` 汇总表（在全文末尾）
4. 每张表的 7 列表格下方可附文字段落：保护字段合法值域、聚合建议

**禁止出现的独立小节：**
- `## 常量候选列`
- `## 聚合方式建议`
- `## 数据质量问题`
- `## 特征衍生思路`
- `## 宽表构建方案`
- 任何 `## 数字.` 编号的话题分组小节

---

## 每张源表的 7 列表格模板

**每张表必须在 7 列表格前包含一行概览信息**，格式固定为：

```markdown
**表名**：<table_name>，**业务含义**：<一句话描述>，**粒度**：<1行=1用户/1行=1行为记录/1行=1游戏>，**主键**：<列名>，**行数**：<N>，**用户数**：<N>，**分类**：<1:1用户表 / 1:N行为表 / 游戏维度表>
```

示例：

```markdown
**表名**：user_info，**业务含义**：用户基本信息与付费转化标签，**粒度**：1行=1用户，**主键**：usid，**行数**：100000，**用户数**：100000，**分类**：1:1用户表
```

概览行后紧接 7 列表格。表头必须逐字照抄，禁止改写列名，禁止增减列数。

```markdown
### <table_name>（N 列）

| 特征名 | 数据类型 | 空值率 | 唯一值 | 特征含义 | 样例值 | 分布分析 |
|--------|---------|--------|--------|---------|--------|---------|
```

**7 列填写规范：**

| 列名 | 填写要求 |
|------|---------|
| 特征名 | 原始字段名 |
| 数据类型 | ClickHouse 数据类型（String / Int32 / Float64 等） |
| 空值率 | 百分比，如 `0%` / `12.3%`，每字段单独给出。**禁止**写成 `空率` 或 `缺失率` |
| 唯一值 | n_unique；n_unique ≤ 10 时列出全量取值及频数，如 `3（男:50000, 女:50000, 未知:0）` |
| 特征含义 | **优先从 `step1_output_meta.json` 中对应字段的 `description` 取值**（如 `"90天电影列表"`、`"30天快应用使用时长"`）。仅在 meta 中无描述时，才可从列名字面或 ClickHouse 样本值自行推断。**禁止机械复读字段名**（如 `contentbehv_fact_movies_list_90d_u字段`） |
| 样例值 | 至少 3 个非空示例值，逗号分隔 |
| 分布分析 | 格式强制：`constant:<true\|false\|semantic>, delimiter:<"#"\|"^"\|null>` |

**分布分析列取值为：**
- `constant:true, delimiter:null` → 仅一个非空值
- `constant:false, delimiter:null` → 正常多值字段，无分隔符
- `constant:false, delimiter:"#"` → 多值特征，`#` 分隔
- `constant:false, delimiter:"^"` → 多值特征，`^` 分隔
- `constant:semantic, delimiter:null` → 语义常量

**示例行：**

| 特征名 | 数据类型 | 空值率 | 唯一值 | 特征含义 | 样例值 | 分布分析 |
|--------|---------|--------|--------|---------|--------|---------|
| usid | String | 0% | 100000 | 用户唯一标识，主键 | 1, 2, 3 | constant:false, delimiter:null |
| label | UInt8 | 0% | 2（0:90000, 1:10000） | 付费标签，0=非付费，1=付费 | 0, 1 | constant:false, delimiter:null |
| age | String | 0% | 7（18岁以下:5000, 18~24岁:18000, 25~34岁:35000, 35~44岁:25000, 45~54岁:12000, 55~64岁:4000, 65岁以上:1000） | 用户年龄段 | 25~34岁, 18~24岁, 45~54岁 | constant:false, delimiter:null |
| gender | String | 2.1% | 3（男:48900, 女:49000） | 用户性别 | 男, 女 | constant:false, delimiter:null |
| game_interest_play_u | String | 5.0% | 55097 | 游戏玩法兴趣标签，#分隔的多值字段 | 沙盒#冒险#象棋, 射击#闯关, (空) | constant:false, delimiter:"#" |
| care_notification_up | String | 3.0% | 99279 | 关注通知用户ID，^分隔的多值列表 | 5815845^9258073, (空) | constant:false, delimiter:"^" |
| active_duration_dev | String | 100% | 1 | 常量空值，无业务意义 | (空) | constant:true, delimiter:null |
| amount_3day | Float64 | 0% | 1 | 3日金额，全为0常量 | 0 | constant:true, delimiter:null |

---

## 列表字段检测结果（全文末尾必须存在）

所有字段画像表格写完、全文档收尾前，必须追加此汇总表。供 step2_3 机器解析。

```markdown
## 列表字段检测结果

| 字段名 | 所在表 | list_delimiter |
|--------|--------|---------------|
| game_interest_play_u | user_info | "#" |
| care_notification_up | user_info | "^" |
| pay_seq | user_info | "#" |
| usid | user_info | null |
```

- 所有 `n_unique > 5` 的 String 字段各占一行
- `list_delimiter` 取值：`"#"` / `"^"` / `null`（三选一，带双引号）
- 此表内容必须与各表"分布分析"列中 delimiter 值完全一致

---

## 写完后必须执行的数据准确性验证

除了格式自检（grep 门禁），写完后还必须执行**数据准确性交叉验证（四维检查）**：

1. **delimiter 真伪**：标记 `delimiter:"#"` 的字段，其 `step2_0_column_profile.sample_values` 必须含 `#`；标记 `"^"` 的必须含 `^`
2. **n_unique 真伪**：抽查低基数字段的 md "唯一值"列与 `step2_0_column_profile.n_unique` 是否一致
3. **missing_rate 真伪**：高缺失率字段（>30%）的 md "空值率"列与 `step2_0_column_profile.missing_rate` 是否一致（不得被 LLM 抹零）
4. **sample_values 真伪**：抽查 md "样例值"列与 `step2_0_column_profile.sample_values` 是否一致

反面案例：223311 运行中 agent 执行了 delimiter 交叉验证但未检查 null_rate/n_unique，导致 `social_interest_strange_social_app_u` 真实 null_rate=95.91% → md 编造为 0%、`sport_interest_ball_game_u` 真实 n_unique=2 且含 # → md 编造为 n_unique=6 单值等级，全部通过门禁。

本验证步骤的详细流程见 `scripts/step2_0_format_gate.md` 步骤 3。

---

## 📋 写完后的格式门禁（7 条 grep 自检）

对刚写入的 `step2_0_source_data_analyze.md` 依次执行以下 7 条 grep：

```bash
# 1. 表头 7 列完整性（必须返回非空，count > 0）
grep -c "特征名.*数据类型.*空值率.*唯一值.*特征含义.*样例值.*分布分析" step2_0_source_data_analyze.md

# 2. 分布分析列格式（必须返回非空，count > 0）
grep -c "constant:\(true\|false\|semantic\), delimiter:" step2_0_source_data_analyze.md

# 3. 列表字段汇总表（必须返回非空，count > 0）
grep -c "## 列表字段检测结果" step2_0_source_data_analyze.md

# 4. 禁止话题分组小节（必须返回空，count == 0）
grep -c "^## [0-9]\+\." step2_0_source_data_analyze.md

# 5. 空值率笼统概览（必须返回空，count == 0）
grep -c "所有.*空值\|所有.*缺失\|所有字段.*0%" step2_0_source_data_analyze.md

# 6. 逐表概览行（必须返回非空，count ≈ 源表总数，如 25）
grep -c "业务含义.*粒度.*主键.*行数.*用户数.*分类" step2_0_source_data_analyze.md

# 7. 特征含义列机械复读（必须返回空，count == 0）
#   拦截 "contentbehv_fact_movies_list_90d_u字段" 等只加"字段"后缀的模式
#   特征含义必须从 step1_output_meta.json 的 description 取值，禁止机械复读
grep -c "[a-z]_[a-z].*字段" step2_0_source_data_analyze.md
```

**不通过处理**：任何一条不通过 → 立即 `delete` 该文件，整体重写，重写后重新 grep。不可"手动修几处"敷衍。
