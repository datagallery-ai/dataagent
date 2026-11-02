## 【DataTaskIR 强制约束说明】
以下DataTaskIR记录了此任务的**已确认口径约束**，你在校验过程中**必须严格遵循**，如果有冲突，以DataTaskIR记录的为准。
特别地，最终输出列权威清单 =「特征主键」前缀列 +「输出字段定义」业务/指标列：INSERT SELECT 的输出列（不含分区列）必须先按 feature_key.components 顺序输出主键列（即使它们未出现在 output_fields 中），再严格按 output_fields 中的非主键字段依次输出（个数、顺序、名称、类型）；多维交叉已展开的每条 fields 项对应一列，不得合并或漏项。若各输出列标注了不同的 aggregation_grain，还必须逐列按该粒度聚合，禁止统一成单一 GROUP BY。多输出、少输出、改名或粒度错用一律视为**硬性错误**，必须要求修正，不得以"建议与业务确认"等疑问语气放过。
见本次输入 data_task_ir 字段。

---

## 校验标准

### 质量门禁（8 类质量自检表）
按以下 8 个质量主题做自检：（注：若DataTaskIR功能未启用，跳过下方相关的检查项）

1. **空值判断**：区分过滤和填充；业务主键、设备 ID、业务对象 ID、分类维度、派生维度、JOIN key 缺失时默认过滤，除非需求明确保留。

2. **表名正确性**：每张源表都有元数据证据和选择理由。

3. **字段名正确性**：每个源字段都与真实 schema 对齐。

4. **条件完整性**：时间窗口、事件类型、状态码、回调结果、业务过滤、TopN、历史截止条件逐条落入 SQL。

5. **稳定排序**：序列、TopN、最近记录、去重保留一条记录必须有确定性排序和稳定 tie-breaker，必要时先聚合到唯一粒度；去重口径优先使用原始业务键，派生键只能在证据证明等价时替代。

6. **JOIN 类型**：明确 JOIN 的业务类型。事实表关联维表必须使用 LEFT JOIN，任何情况下（包括过滤空值或源表已聚合时）都不能使用 INNER JOIN，这是为了避免丢弃事实表中未匹配的记录。

7. **业务逻辑完整性**：需求表、DDL、INSERT 三者在粒度、字段、公式、分隔符、精度、排序、去重、TopN 上一一对应。

8. **口径冻结一致性**：最终 SELECT/GROUP BY、JOIN、窗口、排序、去重、空值策略、枚举取值策略必须与DataTaskIR记录一致；任何偏离都必须有证据或 HITL 结论。**其中最终输出列权威清单 = 特征主键前缀 + 输出字段定义：INSERT SELECT 输出列（不含分区列）必须先包含已确认 feature_key 的全部 components（即使未写入 output_fields），再包含 output_fields 中的全部非主键业务/指标列（多维交叉展开后的每一项各占一列）；列数、顺序、名称、类型不符都是硬性错误。若各输出列标注了 aggregation_grain（或 aggregation_stages 分列了不同 group_by），还必须逐列核对聚合粒度，禁止用单一全局 GROUP BY 覆盖不同粒度特征；多粒度漏写、写错或统一成公共键均为硬性错误，必须列入问题并要求修正，不得作为"建议确认"的软问题放过。**

### Gate Check（必须首先验证，不通过则停止交付并修正）
- [ ] **GATE-1**: INSERT 以 `INSERT OVERWRITE ... PARTITION ...` 开头，且 SELECT 中不输出时间分区列
- [ ] **GATE-2**: 序列分隔符一致性校验。格式类型：
  - 简单列表型：序列中直接为元素时，元素和元素之间使用 `,` 分隔。例如 `elem1,elem2,elem3`
  - 键值对型：序列中为键值对时，键值对之间使用 `;` 分割，键和值之间使用 `:` 分割，值之间使用 `,` 分隔。例如 `键1:值1,值2;键2:值3`
  - 注意：键值对型中若值为单值（例如 `键1:值1`），则不存在值间逗号不视为违规。
- [ ] **GATE-3**: 输出字段一致性校验（以 DataTaskIR「特征主键 + 输出字段定义」为权威）。INSERT SELECT 的输出列（不含分区列）必须：先按 feature_key.components 顺序包含全部已确认主键列（即使未出现在 output_fields 中），再按 output_fields 顺序包含全部非主键业务/指标列；字段个数、顺序、字段名、类型均须一致。多维交叉场景下 output_fields 的每一项必须各占一列，禁止合并窗口/指标导致少列。若 IR 为输出列标注了 aggregation_grain（或 aggregation_stages 给出不同 group_by），还必须逐列核对聚合粒度，禁止用单一全局 GROUP BY 覆盖不同粒度特征。IR 权威清单外的字段禁止出现；权威清单内的字段禁止遗漏。任一不一致即为 GATE 不通过，必须在 gate_failures 中明确指出并停止交付修正。

### 其他检查项
- [ ] INSERT SELECT 不输出分区列
- [ ] DDL 字段数 = INSERT SELECT 输出列数（不含分区列）
- [ ] 输出列与 DataTaskIR 最终输出列权威清单严格一致：INSERT SELECT 输出列（个数/顺序/名称/类型）= 已确认特征主键前缀列 + output_fields 非主键业务/指标列；主键列即使未写入 output_fields 也必须输出；多维交叉展开的每一项不得漏列或合并
- [ ] 逐特征聚合粒度一致：若 output_fields.aggregation_grain 或 aggregation_stages[].group_by 已确认，各输出列的 GROUP BY / 预聚合粒度必须与之逐列对齐，禁止用单一全局粒度覆盖不同特征
- [ ] 每张目标表的 DDL 字段类型与其 INSERT 输出列一一对齐（按位置）
- [ ] DDL 字段类型仅使用白名单：STRING、TINYINT、SMALLINT、INT、BIGINT、DECIMAL(p,s)；分区列仅允许 `pt_* string`
- [ ] 每条 INSERT 仅 1 条主语句（可含子查询/UNION ALL）
- [ ] 比率口径已声明（0~1 比例值，非百分比）
- [ ] 率类指标分母保护：IF(分母>0, 分子/分母, 0)
- [ ] 字符串类型字段使用 `!bicoredata.IsEmpty(field)` 判断；不要只写 `field IS NOT NULL`，因为空字符串也应视为空。
- [ ] 时间窗口分区过滤完整性：从分区表读取数据时，WHERE 条件必须包含分区过滤条件（如 pt_d、pt_h 等），避免全量扫描
- [ ] 时间窗口调度变量格式正确性：
   - 近 N 天包含当天：pt_d <= '$date' AND pt_d > '${start_time,-N,yyyyMMdd}' 或 pt_d <= '$date' AND pt_d >= '${start_time,-N+1,yyyyMMdd}'
   - 近 N 天不包含当天：pt_d < '$date' AND pt_d >= '${start_time,-N,yyyyMMdd}' 或 pt_d < '$date' AND pt_d > '${start_time,-N-1,yyyyMMdd}'
- [ ] 维表时间窗口语义正确：
   - 如果是维表中的天级增量表（_dm 后缀），用户未明确要求时，需要结合任务的语义判定取当天数据还是取多天数据。在取多天数据时，要对数据做去重，数据重复时一般保留最新分区的数据即可。
   - 如果是维表中的天级全量表（_ds 后缀），一般只要取 pt_d = '$date' 即可。
- [ ] JOIN 检查：每个 JOIN 的两侧子查询/CTE 建议在 JOIN 键上已做 GROUP BY 或可证明唯一（维表主键），禁止明细 N:N 直连

### 交付前检查项
- [ ] JOIN 前必须已做预聚合（多事实表 UNION ALL 模式）
- [ ] 字段命名遵循领域词根表（entity_scene_metric_window 格式）
- [ ] 需求表中每个字段标注了来源表字段（含数据库名）
- [ ] 8 类关键问题已逐项自检：空值判断、表名错误、字段名错误、条件缺失、稳定排序、JOIN 类型、业务逻辑缺失、口径冻结一致性
- [ ] 源表和源字段均有搜索工具证据
- [ ] 涉及已有指标、口径、统计周期、主体/维度/场景的逻辑均已用 metadata_recall 检索表列信息，并说明复用或不复用理由
- [ ] 涉及平台 UDF 的逻辑均已用 metadata_recall 确认函数名和用法
- [ ] 所有文件均已通过 write_file 写入 workspace

### SQL 交付前验证（强制）
- 如有多张目标表，将各 DDL 以 `-- === DDL FILE BOUNDARY ===` 分隔合并，INSERT 同理
- 可在 context 参数中补充目标粒度、源表清单等上下文信息
- 如果用户只提供了 DML/DDL 其一，DDL+DML 联合校验的规则（如"DDL 字段数 = INSERT 输出列数"）不适用，可以跳过

---


输出继续使用 warnings/suggestions JSON。GATE 失败项及必需修正写入 warnings（注明 GATE 编号），可选建议写入 suggestions；不得因静态检查通过而省略已发现的 IR 口径冲突。
