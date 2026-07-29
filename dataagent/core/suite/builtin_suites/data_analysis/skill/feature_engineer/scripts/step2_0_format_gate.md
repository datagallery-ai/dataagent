# Step2_0 阶段 2.5：格式门禁

> ⛔ **这是一个独立的 todo item，不可合并到 `阶段3: 撰写 md` 的 todo 中。**
>
> 已知故障：051214 运行中 agent 将 `(含格式门禁)` 当作语义装饰吃掉，一个 `complete_current_todo` 跳过了全部校验。
>
> **执行顺序（严格）：**
> 1. `read_file("scripts/step2_0_source_data_analyze_template.md")` — 刷新格式记忆
> 2. 按模板格式撰写（或重写）`step2_0_source_data_analyze.md`
> 3. 立即执行以下 7 条 grep，不通过 → `delete` 文件 → 回到步骤 2 重写 → 重新 grep
> 4. grep 全部通过后执行数据准确性交叉验证（delimiter + null_rate + n_unique + sample_values 四维，对照 `step2_0_column_profile` 表），不通过 → `delete` 文件 → 回到步骤 2 重写
> 5. **全部通过后**才能标记此 todo 为完成

---

## 步骤 1：读取输出格式模板

```
read_file("scripts/step2_0_source_data_analyze_template.md")
```

该模板包含 `step2_0_source_data_analyze.md` 的唯一合法格式：

- 文档结构要求（逐表独立 `### <table_name>` 节 + 末尾 `## 列表字段检测结果`）
- 7 列表格模板：`特征名 | 数据类型 | 空值率 | 唯一值 | 特征含义 | 样例值 | 分布分析`
- 7 条示例行（覆盖 `constant:true/false/semantic` + `delimiter:"#" / "^" / null` 全量取值）
- ⛔ 禁止清单（独立话题小节 `## 常量候选列` / `## 聚合方式建议` 等）

---

## 步骤 2：写完文件后执行 7 条 grep 硬门禁

对刚写入的 `step2_0_source_data_analyze.md` 依次执行：

```bash
grep -c "特征名.*数据类型.*空值率.*唯一值.*特征含义.*样例值.*分布分析" step2_0_source_data_analyze.md
grep -c "constant:\(true\|false\|semantic\), delimiter:" step2_0_source_data_analyze.md
grep -c "## 列表字段检测结果" step2_0_source_data_analyze.md
grep -c "^## [0-9]\+\." step2_0_source_data_analyze.md
grep -c "所有.*空值\|所有.*缺失\|所有字段.*0%" step2_0_source_data_analyze.md
grep -c "业务含义.*粒度.*主键.*行数.*用户数.*分类" step2_0_source_data_analyze.md
grep -c "[a-z]_[a-z].*字段" step2_0_source_data_analyze.md
# Check 8: table_profile SQL 必须包含 classification 列
# 若返回 0 → agent 展开 SQL 时漏掉了 CASE WHEN，需回退重写
grep -cF "AS classification" step2_0_source_data_analyze_expanded.sql || grep -cF "AS classification" step2_0_source_data_analyze_submit.sql 2>/dev/null || echo "0"
```

- Check 1~3 + 6 + 8**必须返回非空**，Check 6 计数应 ≈ 源表总数（如 25）
- Check 4、5、7**必须返回空**（无违规）
- Check 7 拦截 `特征含义` 列机械复读字段名（如 `contentbehv_fact_movies_list_90d_u字段`）——必须从 `step1_output_meta.json` 的 `description` 取值
- Check 8 拦截 的 未展开——grep 确认已提交的 SQL 文件含 `AS classification`
- 任何一条不通过 → `delete` 文件，整体重写，重新 grep。禁止手动修几处敷衍
- 全部通过后才能进入数据准确性交叉验证

---

## 步骤 3：数据准确性交叉验证（delimiter + null_rate + n_unique 必须与 step2_0_column_profile 一致）

> ⛔ **幻觉复发故障**：
> - 062650 运行：LLM 对 `audio_interest_long_video_u`（真实 n_unique=6, sample_values="较高,无兴趣,中,高,极高"）编造 `n_unique=32415` 和 `"电视剧#电影..."` 后误标 `delimiter:"#"`
> - 223311 运行：LLM 编造了 `social_interest_strange_social_app_u` 的 null_rate=0%（真实95.91%）、`game_interest_theme_u` 的 null_rate=0%（真实64.98%）、`sport_interest_ball_game_u` 的 n_unique=6（真实2，且含#），因数据交叉验证只查 delimiter 不查 null_rate/n_unique 而全部通过
>
> grep 格式门禁只能查格式，不能查数据真伪。本步骤验证 md 中 **delimiter、null_rate、n_unique、sample_values** 三项数据是否与 ClickHouse 真实值一致。

### 验证流程（7 个子步骤，全部通过才算门禁通过）

**3a. 查询 step2_0_column_profile 全量数据（只需执行一次，供 3b~3f 共用）：**

通过 `submit_resource_job` + `collect_job` 提交并获取结果：

```sql
SELECT table_name, column_name, missing_rate, n_unique, sample_values
FROM {output_database}.step2_0_column_profile
ORDER BY table_name, column_name
```

**将此查询结果保存为 `column_profile_reference.txt`**（供后续子步骤 grep 对比）。

---

**3b. delimiter 真伪校验（防假阳性——标了 `#` 但真实无 `#`）：**

```bash
# 提取 md 中 ## 列表字段检测结果 里 delimiter 不为 null 的字段名
rg '^\| (.+?) \| .+? \| "(#|\^)" \|' step2_0_source_data_analyze.md
```

若返回空 → 无字段标记为多值，跳过 3b。

对每个标记 `delimiter:"#"` 的字段，在 `column_profile_reference.txt` 中查找该行，确认 `sample_values` **含 `#`** 字符。
对每个标记 `delimiter:"^"` 的字段，确认 `sample_values` **含 `^`** 字符。

- 任一不匹配 → `delete` 文件 → 回到步骤 2 重写

---

**3b2. ⛔ 假阴性检测（新增——真实含 `#`/`^` 但漏标为 null）：**

> **已知复发故障 0728-6**：`game_interest_style_u`（n_unique=1100）、`game_interest_theme_u`（12327）、`social_interest_strange_social_app_u`（1395）三个字段的真实值含 `#`，但 md 报告标记为 `delimiter:null`。column_profile 的 `sample_values` 因抽样偏差未捕获 `#`（碰巧抽了单值样本），step2_3 因此不执行 splitByChar，三个字段原样透传进 CSV。

**检测方法——采样偏差兜底：从 ClickHouse 源表直接查询，不依赖 column_profile.sample_values。**

从 md 中筛选满足以下条件的字段（高危漏检候选）：
- `## 列表字段检测结果` 中 `list_delimiter = null`
- `n_unique > 100` 且数据类型为 String
- 字段名含 `_interest_`、`_list_`、`_fact_`、`_used_` 等疑似多值标记

对这些候选字段，通过 ClickHouse `submit_resource_job` 提交以下查询——直接从源表取非空样本：

```sql
SELECT DISTINCT toString({col}) AS val
FROM {source_database}.{table}
WHERE toString({col}) != '' AND {col} IS NOT NULL
LIMIT 20
```

**逐字段检查返回的 20 个样本值中是否含 `#` 或 `^`：**

- 发现含 `#` 或 `^` → **漏检！** `delete` 文件 → 回到步骤 2 重写，将该字段的 `delimiter` 修正为 `"#"` 或 `"^"`
- 20 个样本均不含 `#`/`^` → 通过

> **为什么不能只靠 column_profile.sample_values**：sample_values 是 step2_0 阶段 2 执行 `arraySlice(arrayDistinct(groupArray(...)), 1, 5)` 的 TOP-5 采样，存在抽样偏差。`game_interest_style_u` 的 TOP-5 样本恰为 `Q版, 写实, 暗黑, 日韩, 卡漫`——全是单值不含 `#`，但该字段 1100 个唯一值中绝大多数是 `Q版#卡漫#日韩` 格式。本步骤的 `LIMIT 20` 从源表直接取 DISTINCT 非空值，覆盖率更高。

---

**3c. n_unique 真伪校验（新增）：**

从 md 中逐表逐字段提取"唯一值"列的数值，与 `column_profile_reference.txt` 中同字段的 `n_unique` 对比。

操作方法：对 `column_profile_reference.txt` 中 `n_unique ≤ 50` 的字段（低基数且容易编造），逐一打开 md 中对应表的字段行，肉眼比对 md "唯一值"列与 reference 的 `n_unique` 是否一致。**至少抽查 10 个字段，优先抽 String 类型、n_unique ≤ 20 的字段**。

- 任何字段数值不一致 → **LLM 编造了 n_unique**，`delete` 文件 → 回到步骤 2 重写
- 全部一致 → 通过

---

**3d. missing_rate 真伪校验（新增）：**

从 md 中逐表逐字段提取"空值率"列的数值（如 `64.98%`），与 `column_profile_reference.txt` 中同字段的 `missing_rate` 对比。

操作方法：从 `column_profile_reference.txt` 中筛出 `missing_rate > 0.3` 的字段（高缺失率容易被 LLM 抹零），逐一打开 md 中对应表的字段行，肉眼比对 md "空值率"列与 reference 的 `missing_rate` 是否一致（允许 ±1% 的四舍五入差异）。

- 任何字段的 missing_rate 被大幅改写（如真实 64.98% → md 写 0%）→ `delete` 文件 → 回到步骤 2 重写
- 全部一致 → 通过

---

**3e. sample_values 真伪校验（新增）：**

从 md 中任意抽取 5 个字段，肉眼比对 md "样例值"列与 `column_profile_reference.txt` 中同字段的 `sample_values` 是否一致。

- `sample_values` 中逗号分隔的值应原样出现在 md "样例值"列中（允许截断过长列表）
- 若 md 的样例值出现了 `sample_values` 中不存在的值（如真实 `"无兴趣,中,极高"` 但 md 写成 `"电视剧#电影, 综艺#纪录片"`）→ **LLM 编造了样本值**，`delete` 文件 → 回到步骤 2 重写

---

**3f. ⛔ 表分类真伪校验（新增——禁止 LLM 猜分类，硬 SQL 规则为准）：**

> **已知复发故障 013113**：LLM 将 `list_detail_info`（行数=30000, usid n_unique=30000）和 `game_statistics_push`（61691=61691）误判为 1:N 行为表，导致 step2_3 对无需聚合的表做了多余的 GROUP BY。
>
> **铁律**：`n_unique(主键) = 行数 ↔ 1:1`。必须由 SQL 计算，禁止 LLM 手工判断。

通过 `submit_resource_job` + `collect_job` 提交并获取：

```sql
SELECT
    table_name,
    total_rows,
    unique_user_id,
    CASE
        WHEN unique_user_id IS NULL THEN '维度表'
        WHEN unique_user_id = total_rows THEN '1:1用户表'
        ELSE '1:N行为表'
    END AS hard_classification
FROM {output_database}.step2_0_table_profile
ORDER BY table_name
```

逐表打开 md 中该表的概览行（`**分类**：<值>`），比对 md 分类与 `hard_classification` 是否一致：

- 任一不一致 → **LLM 猜错了**，`delete` 文件 → 回到步骤 2 重写
- 全部一致 → 通过

---

### 反面案例速查

| 日期 | 字段 | column_profile 真实值 | md 编造值 | 被漏检的项 |
|------|------|---------------------|----------|-----------|
| 062650 | audio_interest_long_video_u | n_unique=6, sample="较高,无兴趣,中" | n_unique=32415, sample="电视剧#电影..." | delimiter（触发） |
| 223311 | social_interest_strange_social_app_u | missing_rate=95.91% | 0% | **missing_rate** |
| 223311 | game_interest_theme_u | missing_rate=64.98% | 0% | **missing_rate** |
| 223311 | sport_interest_ball_game_u | n_unique=2, sample含# → delimiter:# | n_unique=6, delimiter:null | **n_unique + delimiter** |
| 223311 | game_interest_user_lifetime_u | n_unique=5 | n_unique=69867 | **n_unique** |
| 0728-6 | game_interest_style_u | 真实含 #（`Q版#卡漫#日韩`），n_unique=1100 | delimiter:null（因 sample_values 抽样偏差捕获了单值 `Q版,写实`） | **假阴性（新增 3b2）** |
| 0728-6 | game_interest_theme_u | 真实含 #，n_unique=12327 | delimiter:null | **假阴性** |
| 0728-6 | social_interest_strange_social_app_u | 真实含 #，n_unique=1395 | delimiter:null | **假阴性** |
| 013113 | list_detail_info | n_unique(usid)=30000=30000行 → 1:1 | md 分类=1:N行为表 | **表分类（新增 3f）** |
| 013113 | game_statistics_push | n_unique(usid)=61691=61691行 → 1:1 | md 分类=1:N行为表 | **表分类** |

---

## 为什么需要这个门禁

过去 4 次连续运行（030353、031729、033404、035428）全部因 Phase3 前未重读格式模板而写出违规产物。失败模式已固化：Phase2 的 100+ 次 ClickHouse 交互将格式约束冲刷出上下文窗口，agent 进入 Phase3 时进入"数据描述模式"，按自由体裁写报告。本门禁通过在 Phase2 pipeline 末尾嵌入格式读取步骤，确保在窗口衰减发生前刷新格式记忆。

---

## 步骤 4：列表字段识别（在撰写字段画像时同步执行）

> ⛔ **必做，不可跳过。** 在撰写每张表的字段画像表格时，必须逐表逐字段完成列表字段检测，将结果写入"分布分析"列的 `delimiter` 字段。
>
> 反面案例：agent 曾对 `game_interest_play_u`（含 `RPG#FPS#MOBA`）、`care_notification_up`（含 `"new_feature^system^event"`）等字段仅标注 "high cardinality"，未识别分隔符，导致 step2_3 无法触发 splitByChar 处理。

**检测流程（严格按顺序，不可跳过）：**

1. **筛选候选字段**：从 `step2_0_column_profile` 中筛选满足以下任一条件且数据类型为 String 的字段（排除 `<user_id>`、`<label>` 等保护列）：
   - `n_unique > 5`
   - `n_unique ≤ 5` 但 `sample_values` 中明确含 `#` 或 `^`（低 n_unique 往往源于大部分行空值，非空值仍可能是多值列表字段）

2. **查询样本值**：对每个候选字段，通过 ClickHouse MCP `submit_resource_job` 提交：
   ```sql
   SELECT DISTINCT toString({col}) AS val
   FROM {source_database}.{table}
   WHERE {col} IS NOT NULL AND toString({col}) != ''
   LIMIT 10
   ```
   **必须记录每个字段的查询结果**——这些样本值将作为 md "样例值"列的素材，不能编造

3. **交叉校验 step2_0_column_profile（必做，不可跳过）**：对每个候选字段，将其 `SELECT DISTINCT` 样本值查询结果与 `step2_0_column_profile` 中该字段的 `sample_values` 列比对：
   - **`sample_values` 中不含 `#` 或 `^`**（如 `"无兴趣,中,极高,高,较高"`）→ 该字段**不是**列表字段，禁止标记 `delimiter:"#"` 或 `"^"`。**即使 `SELECT DISTINCT` 查询碰巧抽到含 `#` 的样本也要优先相信 column_profile**（column_profile 是全量统计）
   - **`sample_values` 中含 `#` 或 `^`**（如 `"沙盒#冒险#象棋, 射击#闯关"`）→ 该字段确认为列表字段

4. **判定分隔符**，结果直接写入该字段在 7 列表格中"分布分析"列的 `delimiter` 字段：
   - 样本值中出现 `#` → `constant:false, delimiter:"#"`
   - 样本值中出现 `^` → `constant:false, delimiter:"^"`
   - 两种均未出现 → `constant:false, delimiter:null`

5. **写入汇总表**：全文档所有字段写完后，在 `step2_0_source_data_analyze.md` **末尾**必须追加 `## 列表字段检测结果` 汇总表。分隔符列取值严格为三个 token 之一：`"#"` / `"^"` / `null`。**禁止写自然语言描述**（如 `"# 分隔的游戏主题兴趣"`），否则 step2_3 无法 grep 解析。

**Checklist（逐表验收，全部通过后方可进入表分类）：**
- [ ] 已从 `step2_0_column_profile` 筛选出全部 `n_unique > 5` 的 String 字段
- [ ] 每个候选字段已查询至少 10 条非空样本值，已记录查询结果
- [ ] 每个候选字段的样本值已与 `step2_0_column_profile.sample_values` 交叉校验
- [ ] 每张表的字段画像表格"分布分析"列中 `delimiter` 值已填写
- [ ] `step2_0_source_data_analyze.md` 末尾存在 `## 列表字段检测结果` 表格
- [ ] 表格中每个候选字段各占一行，list_delimiter 列仅允许 `"#"` / `"^"` / `null` 三值

---

### 🗄️ 门禁通过后的硬标记

> 全部 7 条 grep + 7 个子步骤通过后，**必须**写入 `gate_data_verified.txt`（内容任意，仅作文件级标记）。
> 此文件是阶段 3 的硬前置——文件不存在 → 阶段 3 被阻塞，强制回退到阶段 2.5。

```bash
echo "PASSED: $(date)" > gate_data_verified.txt
```

---

## 步骤 4：列表字段识别（在撰写字段画像时同步执行）
