# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
import asyncio
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import sqlglot
from loguru import logger
from sqlglot import exp

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.hooks.examples.ir_hooks import get_ir_context
from dataagent.core.managers.llm_manager import ChatModel, llm_manager
from dataagent.core.managers.llm_manager.stream_utils import collect_llm_stream

# ============================================================================
# validate_deliverables orchestration constants
#
# validate_deliverables 把"静态校验 + DataOps 语义校验"合并成单一入口,
# 整个工具在一次会话内最多跑 MAX_VALIDATE_DELIVERABLES_ATTEMPTS 次。
# retry 由 validate_deliverables 在工具层统一调度,
# DataOps wrapper 内部 3 次上限保留作为兜底防御。
# ============================================================================
MAX_VALIDATE_DELIVERABLES_ATTEMPTS = 3
_VALIDATE_DELIVERABLES_STATE_KEY = "validate_deliverables_state"
_DELIVERY_DELIVERABLES_WARNING_FILENAME = "validate_deliverables_warning.md"
_DELIVERY_DELIVERABLES_WARNING_HEADER = (
    "⚠️ 本次交付物未通过 validate_deliverables 校验（重试 {n} 次仍失败），仅作 best-effort，下游使用前必须人工复核"
)
_DELIVERY_DELIVERABLES_REPORT_FILENAME = "validate_deliverables_report.json"

# 质量门禁校验的 system prompt
_QUALITY_GATE_SYSTEM_PROMPT = """你是一位资深数据仓库质量校验专家。你的任务是严格审查 DDL 和 DML 代码，按以下标准进行全面质量检查。

重要原则：
- 不要用猜测补齐业务规则
- 必须基于提供的元数据和代码进行校验
- 对于不确定的情况，应标记为待确认而非直接通过"""

# 质量门禁校验的 user prompt 模板（使用 {user_query}, {ddl_content}, {dml_content}, {metadata_content} 占位）
_QUALITY_GATE_USER_PROMPT_TEMPLATE = """## 用户原始需求
{user_query}

## 待校验代码

### DDL 文件
```sql
{ddl_content}
```

### DML 文件
```sql
{dml_content}
```

## 元数据上下文（可选参考）
{metadata_content}


## 意图理解后给到NL2SQL子Agent的需求细节
{nl2sql_detail}


## 【DataTaskIR 强制约束说明】
以下DataTaskIR记录了此任务的**已确认口径约束**，你在校验过程中**必须严格遵循**，如果有冲突，以DataTaskIR记录的为准。
特别地，最终输出列语义清单 =「特征主键」(identity) 前缀列 + expand(output_features) 业务/指标语义：INSERT SELECT 的输出列（不含分区列）必须先按 identity.components 顺序输出主键列（即使它们未出现在 output_features 中），再覆盖 expand(output_features) 的全部非主键业务/指标语义（个数按展开项计；layout=cartesian 时每项对应一列，不得合并或漏项）。**IR 展开列名仅供主 Agent 参考，不要求 SQL/DDL 字面同名，不得因列名不一致判失败。**若展开后各列 aggregation.grain 不同，还必须逐列按该粒度聚合，禁止统一成单一 GROUP BY。语义漏列/多无关列或粒度错用一律视为**硬性错误**，必须要求修正，不得以"建议与业务确认"等疑问语气放过。
{rendered_ir}

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

8. **口径冻结一致性**：最终 SELECT/GROUP BY、JOIN、窗口、排序、去重、空值策略、枚举取值策略必须与DataTaskIR记录一致；任何偏离都必须有证据或 HITL 结论。**其中最终输出列语义清单 = 特征主键前缀 + expand(output_features)：INSERT SELECT 输出列（不含分区列）必须先包含已确认 identity 的全部 components（即使未写入 output_features），再覆盖 expand(output_features) 的全部非主键业务/指标语义（cartesian 确定性展开后的每一项各占一列）；语义漏列/多无关列是硬性错误。IR 展开列名仅供参考，列名不一致不作为硬性错误。若展开后各列标注了 aggregation.grain，还必须逐列核对聚合粒度，禁止用单一全局 GROUP BY 覆盖不同粒度特征；多粒度漏写、写错或统一成公共键均为硬性错误，必须列入问题并要求修正，不得作为"建议确认"的软问题放过。**

### Gate Check（必须首先验证，不通过则停止交付并修正）
- [ ] **GATE-1**: INSERT 以 `INSERT OVERWRITE ... PARTITION ...` 开头，且 SELECT 中不输出时间分区列
- [ ] **GATE-2**: 序列分隔符一致性校验。格式类型：
  - 简单列表型：序列中直接为元素时，元素和元素之间使用 `,` 分隔。例如 `elem1,elem2,elem3`
  - 键值对型：序列中为键值对时，键值对之间使用 `;` 分割，键和值之间使用 `:` 分割，值之间使用 `,` 分隔。例如 `键1:值1,值2;键2:值3`
  - 注意：键值对型中若值为单值（例如 `键1:值1`），则不存在值间逗号不视为违规。
- [ ] **GATE-3**: 输出字段语义一致性校验（以 DataTaskIR「特征主键 + expand(output_features)」语义清单为准）。INSERT SELECT 的输出列（不含分区列）必须：先按 identity.components 顺序包含全部已确认主键列（即使未出现在 output_features 中），再覆盖 expand(output_features) 的全部非主键业务/指标语义；展开项个数不得少列/合并。layout=cartesian 时展开后的每一项必须各占一列。**IR 参考列名不要求字面一致，不得因改名判 GATE-3 失败。**若展开后各列标注了 aggregation.grain，还必须逐列核对聚合粒度，禁止用单一全局 GROUP BY 覆盖不同粒度特征。语义清单外的无关业务字段禁止出现；清单内语义禁止遗漏。任一语义不一致即为 GATE 不通过，必须在 gate_failures 中明确指出并停止交付修正。

### 其他检查项
- [ ] INSERT SELECT 不输出分区列
- [ ] DDL 字段数 = INSERT SELECT 输出列数（不含分区列）
- [ ] 输出列与 DataTaskIR 最终输出列语义清单一致：INSERT SELECT 须覆盖已确认特征主键前缀 + expand(output_features) 全部业务/指标语义；主键列即使未写入 output_features 也必须输出；cartesian 确定性展开的每一项不得漏列或合并；IR 参考列名不要求同名
- [ ] 逐特征聚合粒度一致：若展开后各列 aggregation.grain 已确认，各输出列的 GROUP BY / 预聚合粒度必须与之逐列对齐，禁止用单一全局粒度覆盖不同特征
- [ ] 每张目标表的 DDL 字段类型与其 INSERT 输出列一一对齐（按位置）
- [ ] DDL 字段类型仅使用白名单：STRING、TINYINT、SMALLINT、INT、BIGINT、DECIMAL(p,s)；分区列仅允许 `pt_* string`
- [ ] 每条 INSERT 仅 1 条主语句（可含子查询/UNION ALL）
- [ ] 比率口径已声明（0~1 比例值，非百分比）
- [ ] 率类指标分母保护：IF(分母>0, 分子/分母, 0)
- [ ] 字符串类型字段使用 `!bicoredata.IsEmpty(field)` 判断；不要只写 `field IS NOT NULL`，因为空字符串也应视为空。
- [ ] 时间窗口分区过滤完整性：从分区表读取数据时，WHERE 条件必须包含分区过滤条件（如 pt_d、pt_h 等），避免全量扫描
- [ ] 时间窗口调度变量格式正确性：
   - 近 N 天包含当天：pt_d <= '$date' AND pt_d > '${{start_time,-N,yyyyMMdd}}' 或 pt_d <= '$date' AND pt_d >= '${{start_time,-N+1,yyyyMMdd}}'
   - 近 N 天不包含当天：pt_d < '$date' AND pt_d >= '${{start_time,-N,yyyyMMdd}}' 或 pt_d < '$date' AND pt_d > '${{start_time,-N-1,yyyyMMdd}}'
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

## 输出要求

请输出严格的 JSON 格式，包含以下字段：

```json
{{
  "gate_passed": true/false,
  "gate_failures": ["失败项描述1", "失败项描述2"],
  "quality_checks": [
    {{
      "category": "质量类别名称",
      "passed": true/false,
      "issues": ["问题描述1", "问题描述2"],
      "suggestions": ["改进建议1"]
    }}
  ],
  "summary": "总体评估摘要"
}}
```

- GATE-1/GATE-2/GATE-3 任一不通过时，gate_passed 必须为 false，并将对应失败项（以"GATE-N: "开头，明确写出失败原因，如"GATE-3: 输出字段一致性校验未通过：输出列多出 app_fourth_class_cn_name"）写入 gate_failures；失败项描述中不要使用"符合/通过"等含通过含义的词。
- 输出字段与 IR「特征主键 + expand(output_features)」语义清单不一致（缺主键列、多无关列、少语义列、cartesian 交叉漏列），或逐特征聚合粒度与展开后各列 aggregation.grain 不一致，必须作为 GATE-3 硬性失败；**仅列名与 IR 参考名不同不得判失败**；禁止以"建议与业务确认"的软性口吻描述。

必须返回合法 JSON，不得包含 markdown 代码块标记或其他文字。"""


def _build_quality_gate_prompt(
    ddl_content: str,
    dml_content: str,
    metadata_content: str,
    user_query: str = "",
    rendered_ir: str = "",
    nl2sql_detail: str = "",
) -> tuple[str, str]:
    """
    构建质量门禁校验的 LLM 提示词。

    Returns:
        tuple[str, str]: (system_prompt, user_prompt)
    """
    user_prompt = _QUALITY_GATE_USER_PROMPT_TEMPLATE.format(
        user_query=user_query if user_query else "(未提供)",
        ddl_content=ddl_content if ddl_content else "(无 DDL 文件)",
        dml_content=dml_content if dml_content else "(无 DML 文件)",
        metadata_content=metadata_content if metadata_content else "(无元数据文件)",
        rendered_ir=rendered_ir if rendered_ir else "",
        nl2sql_detail=nl2sql_detail if nl2sql_detail else "",
    )
    return _QUALITY_GATE_SYSTEM_PROMPT, user_prompt


def _parse_quality_gate_result(raw_result: str, warnings: list[str], include_suggestions: bool = False) -> None:
    """
    解析 LLM 返回的质量门禁校验结果，追加到 warnings 列表。

    Args:
        raw_result: LLM 返回的原始字符串
        warnings: 警告列表，结果会追加到此列表
        include_suggestions: 是否追加"建议"级别的问题；默认 False 不返回
    """
    try:
        # 清理可能的 markdown 代码块标记
        result_str = raw_result
        if result_str.startswith("```"):
            result_str = result_str.split("```")[1] if "```" in result_str else result_str
            result_str = result_str.removeprefix("json\n").removesuffix("```").strip()

        result_json = json.loads(result_str)

        # 解析结果并追加到 warnings
        gate_passed = result_json.get("gate_passed", True)
        gate_failures = result_json.get("gate_failures", [])
        quality_checks = result_json.get("quality_checks", [])
        summary = result_json.get("summary", "")

        # 仅当真正的 GATE 项（GATE-1/GATE-2/GATE-3）失败时才报告 GATE Check 未通过
        gate_specific_failures = []
        gate_all_results = []  # 收集所有 GATE 检查结果（用于需要关注时展示）
        for f in gate_failures:
            if "GATE-1" in f or "GATE-2" in f or "GATE-3" in f:
                gate_all_results.append(f)
                # 检查是否为真正失败：先剔除"未通过/不通过"等否定表述再判断，
                # 避免"校验未通过"因包含"通过"子串而被误判为通过
                negated = f.replace("未通过", "").replace("不通过", "")
                if not any(keyword in negated for keyword in ["符合", "通过", "PASS", "pass", "成功"]):
                    gate_specific_failures.append(f)

        if not gate_passed and gate_specific_failures:
            warnings.append("【LLM质量门禁】GATE Check 未通过，需要修正:")
            for failure in gate_specific_failures:
                warnings.append(f"  - {failure}")
        elif gate_all_results:
            # 有 GATE 检查结果但无失败项时，显示需要关注
            warnings.append("【LLM质量门禁】GATE Check: ⚠ 需要重点关注")
            for result in gate_all_results:
                warnings.append(f"  - {result}")

        for check in quality_checks:
            category = check.get("category", "未知类别")
            passed = check.get("passed", True)
            issues = check.get("issues", [])
            suggestions = check.get("suggestions", [])

            # 过滤符合要求的项
            filtered_issues = [i for i in issues if "符合" not in i]
            filtered_suggestions = [s for s in suggestions if "符合" not in s]

            # passed=True 且无真正问题时跳过；仅当不返回建议或确实无建议时才可跳过
            if passed and not filtered_issues and (not include_suggestions or not filtered_suggestions):
                continue

            status = "✓ 通过" if passed else "⚠ 需要关注"
            warnings.append(f"【LLM质量校验】{category}: {status}")
            for issue in filtered_issues:
                # 去除已包含"问题"的重复前缀
                if issue.startswith("问题"):
                    warnings.append(f"  - {issue}")
                else:
                    warnings.append(f"  - 问题: {issue}")
            if include_suggestions:
                for suggestion in filtered_suggestions:
                    # 去除已包含"建议"的重复前缀
                    if suggestion.startswith("建议"):
                        warnings.append(f"  - {suggestion}")
                    else:
                        warnings.append(f"  - 建议: {suggestion}")

        if summary:
            warnings.append(f"【LLM质量校验】总体评估: {summary}")

    except json.JSONDecodeError:
        logger.warning(f"LLM 质量校验返回无效 JSON: {raw_result[:200]}...")
        warnings.append("【LLM质量校验】LLM 返回无效 JSON，跳过详细检查")
    except Exception as e:
        logger.warning(f"LLM 质量校验执行异常: {e}")
        warnings.append(f"【LLM质量校验】执行异常: {str(e)}")


def _resolve_quality_gate_include_suggestions(_tool_context: ToolExecutionContext | None = None) -> bool:
    """
    读取 agent 配置中 quality gate 是否返回"建议"级别的问题。

    配置键：AGENT_CONFIG.quality_gate_include_suggestions（deagent_config.yaml），
    默认 False（不返回建议，仅返回错误/需要关注的问题）。
    """
    try:
        config_manager = getattr(_tool_context, "config_manager", None)
        if config_manager is None:
            runtime = getattr(_tool_context, "runtime", None)
            if runtime is not None:
                config_manager = getattr(runtime, "config_manager", None)
        if config_manager is None:
            return False
        return bool(config_manager.get("AGENT_CONFIG.quality_gate_include_suggestions", False))
    except Exception as e:
        logger.warning(f"读取 quality_gate_include_suggestions 配置失败: {e}")
        return False


def validate_deliverables(*, query: str = "", _tool_context: ToolExecutionContext | None = None) -> dict[str, str]:
    """
    Check if final deliverables satisfy basic quality requirements **and**
    pass DataOps semantic validation. Call this function when you think you
    have finished all steps. Errors reported from this function are required
    to be fixed, while suggestions reported can be taken into consideration
    at will.

    **Single-entry retry semantics (NEW):**
    - The full check (static checks + DataOps semantic validation) runs as
      one unit. If validation fails, **fix the SQL in workspace** and call
      this tool again.
    - This tool will run **at most MAX_VALIDATE_DELIVERABLES_ATTEMPTS (3)
      times per session**; after that it returns best-effort verdict and
      stops accepting further calls.
    - The internal per-SQL DataOps retry budget is preserved as a safety
      net (wrapper enforces its own 3-attempt cap) but is normally unused
      because each ``validate_deliverables`` invocation starts the DataOps
      wrapper from attempt=1.

    Args:
        query: The user's original requirement text, used by the LLM quality gate to verify
            that DDL/DML aligns with the actual requirement and avoid false positives.
            - Single-turn conversation: pass the user's query as-is.
            - Multi-turn conversation: pass empty string (do NOT pass query).

    Returns:
        dict[str, str], original and frontend message containing if final deliverables have passed
            basic quality checks and any details.
    """
    workspace_dir = _tool_context.runtime.workspace_dir

    # add_column 场景产出 alter_*.sql，不适用本工具的校验规则，直接拒绝
    if _is_alter_scenario(workspace_dir):
        msg = "当前为 ALTER/ADD_COLUMN 场景，validate_deliverables 不适用。ALTER 场景无需调用此工具，请跳过此步骤。"
        return {"original_msg": msg, "frontend_msg": msg}

    state = _load_validate_deliverables_state(_tool_context)

    # [重试上限] 单次会话最多跑 MAX_VALIDATE_DELIVERABLES_ATTEMPTS 次。
    # state.attempts 已经在 _bump_attempt 中预递增过；如果超过上限说明 agent
    # 不顾警告还在继续调用，直接返回 best-effort 拒绝。
    if state["attempts"] > MAX_VALIDATE_DELIVERABLES_ATTEMPTS:
        msg = (
            f"validate_deliverables 已达到本会话最大重试上限 "
            f"({MAX_VALIDATE_DELIVERABLES_ATTEMPTS} 次)，不再接受新调用。"
            "如需重新启动校验，请清空 runtime cache 或开新 session。"
        )
        logger.warning(f"[validate_deliverables] {msg} state={state}")
        return {"original_msg": msg, "frontend_msg": msg}

    current_attempt = state["attempts"]
    logger.info(f"[validate_deliverables] === START attempt={current_attempt}/{MAX_VALIDATE_DELIVERABLES_ATTEMPTS} ===")

    include_suggestions = _resolve_quality_gate_include_suggestions(_tool_context)

    # === Step 1: 静态校验（DDL/DML 文件存在性 + 规范 + LLM 质量门禁） ===
    static_passed, static_err, static_warn = _validate_deliverables(
        workspace_dir=workspace_dir,
        user_query=query,
        include_suggestions=include_suggestions,
        _tool_context=_tool_context,
    )

    # 静态校验失败 → 不跑 dataops（先解决 schema/语法问题），保存状态后让 agent 修
    if not static_passed:
        state["last_static_passed"] = False
        state["last_static_err"] = static_err
        state["last_static_warn"] = static_warn
        state["last_failure_kind"] = "static"
        state["last_updated_at"] = time.time()
        _save_validate_deliverables_state(_tool_context, state)

        out = (
            f"[validate_deliverables attempt {current_attempt}/"
            f"{MAX_VALIDATE_DELIVERABLES_ATTEMPTS}] 静态校验不通过，请修复后再次调用。\n"
            f"{static_err}"
        )
        if static_warn:
            out += "\n" + static_warn
        logger.info(f"[validate_deliverables] === END attempt={current_attempt} static=FAIL ===")
        return {"original_msg": out, "frontend_msg": out}

    # === Step 2: DataOps 语义校验（仅静态通过后才执行；DDL+DML 都跑） ===
    DDL, DML = _locate_sql(workspace=workspace_dir)
    dataops_summary = _run_dataops_validation_round(
        ddl_path=DDL,
        dml_path=DML,
        state=state,
        _tool_context=_tool_context,
    )

    dataops_passed = dataops_summary["passed"]
    dataops_err = dataops_summary["error"]
    dataops_warn = dataops_summary["warning"]
    dataops_log_summary = dataops_summary["log_summary"]

    # === Step 3: 汇总结果 ===
    if dataops_passed:
        # 通过 → 清空 cache 里的失败历史（保持状态一致，但允许下次再被覆写）
        state["last_static_passed"] = True
        state["last_static_err"] = ""
        state["last_static_warn"] = static_warn
        state["last_failure_kind"] = None
        state["last_updated_at"] = time.time()
        _save_validate_deliverables_state(_tool_context, state)

        out = (
            f"[validate_deliverables attempt {current_attempt}/"
            f"{MAX_VALIDATE_DELIVERABLES_ATTEMPTS}] ✅ 最终产物校验通过。"
        )
        merged_warn = _merge_warnings(static_warn, dataops_warn)
        if merged_warn:
            out += "\n" + merged_warn
        logger.info(f"[validate_deliverables] === END attempt={current_attempt} result=PASSED ===")
        return {"original_msg": out, "frontend_msg": out}

    # DataOps 失败 → 是否达到本会话上限？
    state["last_static_passed"] = True
    state["last_static_err"] = ""
    state["last_static_warn"] = static_warn
    state["last_failure_kind"] = "dataops"
    state["last_dataops_err"] = dataops_err
    state["last_dataops_log_summary"] = dataops_log_summary
    state["last_updated_at"] = time.time()

    if current_attempt >= MAX_VALIDATE_DELIVERABLES_ATTEMPTS:
        # 已达上限：写 best-effort 报告 + warning 文件 + 返回 best-effort 文案
        report_paths = _dump_validate_deliverables_artifacts(
            state=state,
            workspace_dir=workspace_dir,
            ddl_path=DDL,
            dml_path=DML,
            _tool_context=_tool_context,
        )
        if report_paths.get("report_path"):
            state["last_report_path"] = report_paths["report_path"]
        if report_paths.get("warning_path"):
            state["last_warning_path"] = report_paths["warning_path"]
        _save_validate_deliverables_state(_tool_context, state)

        out = (
            f"[validate_deliverables attempt {current_attempt}/"
            f"{MAX_VALIDATE_DELIVERABLES_ATTEMPTS}] ❌ DataOps 校验失败且已达到"
            f"本会话最大重试上限 ({MAX_VALIDATE_DELIVERABLES_ATTEMPTS} 次)，"
            "本次交付仅作 best-effort，下游使用前必须人工复核。\n"
        )
        out += f"- DataOps 错误: {dataops_err}\n" if dataops_err else ""
        if dataops_log_summary:
            out += f"- 日志分析摘要: {dataops_log_summary}\n"
        merged_warn = _merge_warnings(static_warn, dataops_warn)
        if merged_warn:
            out += "\n" + merged_warn
        if report_paths.get("warning_path"):
            out += (
                f"\n详细报告已写入 `{report_paths['warning_path']}`，"
                "请把本次提交到 workspace 的 SQL 作为最终交付物（best-effort）。"
            )
        logger.warning(
            f"[validate_deliverables] === END attempt={current_attempt} result=BEST_EFFORT_FAIL "
            f"report={report_paths} ==="
        )
        return {"original_msg": out, "frontend_msg": out}

    # 还未到上限：保存历史，让 agent 修复 SQL 后再次调用本工具
    _save_validate_deliverables_state(_tool_context, state)

    out = (
        f"[validate_deliverables attempt {current_attempt}/"
        f"{MAX_VALIDATE_DELIVERABLES_ATTEMPTS}] ❌ DataOps 校验失败，请按下面诊断"
        f"修复 workspace 中的 DDL/DML 后再次调用本工具。\n"
    )
    if dataops_err:
        out += f"- DataOps 错误: {dataops_err}\n"
    if dataops_log_summary:
        out += f"- 日志分析摘要: {dataops_log_summary}\n"
    merged_warn = _merge_warnings(static_warn, dataops_warn)
    if merged_warn:
        out += "\n" + merged_warn
    remaining = MAX_VALIDATE_DELIVERABLES_ATTEMPTS - current_attempt
    out += f"\n（剩余可重试次数: {remaining}）"
    logger.info(f"[validate_deliverables] === END attempt={current_attempt} dataops=FAIL ===")
    return {"original_msg": out, "frontend_msg": out}


# ============================================================================
# validate_deliverables orchestration helpers
# ============================================================================


def _new_validate_deliverables_state() -> dict:
    """Initialize a fresh retry state. ``attempts`` 是本工具已累计调用次数，
    由 ``_bump_attempt`` 在加载时预递增，所以新对象此处为 0。"""
    return {
        "attempts": 0,
        "dataops_attempts": [],  # 累计的 dataops 尝试摘要（跨 validate_deliverables 复用）
        "last_static_passed": None,
        "last_static_err": "",
        "last_static_warn": "",
        "last_failure_kind": None,  # "static" | "dataops" | None
        "last_dataops_err": "",
        "last_dataops_log_summary": "",
        "last_report_path": None,
        "last_warning_path": None,
        "last_updated_at": 0.0,
    }


def _load_validate_deliverables_state(_tool_context: ToolExecutionContext) -> dict:
    """从 runtime cache 读取重试状态；如果不存在则初始化并写回。

    调用本函数时已隐含"本工具被调用一次"，所以这里直接预递增 attempts。"""
    runtime = _tool_context.runtime
    state = runtime.get_cache(_VALIDATE_DELIVERABLES_STATE_KEY)
    if not isinstance(state, dict) or "attempts" not in state:
        state = _new_validate_deliverables_state()
    state["attempts"] = int(state.get("attempts", 0)) + 1
    if not isinstance(state.get("dataops_attempts"), list):
        state["dataops_attempts"] = []
    return state


def _save_validate_deliverables_state(_tool_context: ToolExecutionContext, state: dict) -> None:
    """把当前状态写回 runtime cache，供下一次 validate_deliverables 调用读取。"""
    runtime = _tool_context.runtime
    runtime.set_cache(_VALIDATE_DELIVERABLES_STATE_KEY, state)


def _merge_warnings(static_warn: str, dataops_warn: str) -> str:
    """合并静态校验和 DataOps 的 warning 输出，过滤空段。"""
    parts = []
    if static_warn and static_warn.strip():
        parts.append(static_warn.strip())
    if dataops_warn and dataops_warn.strip():
        parts.append(dataops_warn.strip())
    return "\n\n".join(parts)


# ============================================================================
# DDL 预清理
#
# DDL 重跑时 adhoctemp 临时表可能已存在（上次 CREATE 失败但表留下了），
# 直接重跑会报 "Table already exists"。
#
# 预清理由 ``dataops_validate_sql`` 内部的 ``_pre_drop_for_create`` /
# ``_pre_drop_for_create_prod`` 负责 —— 那里会**同步等 DROP 真正执行完**
# 再提交 CREATE。这里曾有一个 fire-and-forget 的并行实现（后台线程提交
# DROP 后立即返回），它有两个问题，故已移除：
#   1. DROP 的实际执行可能晚于 CREATE，于是把刚建好的表删掉；
#   2. 不等完成 → 无法保证 CREATE 前同名表已消失。
# ============================================================================


def _run_dataops_validation_round(
    *,
    ddl_path: Path | None,
    dml_path: Path | None,
    state: dict,
    _tool_context: ToolExecutionContext,
) -> dict:
    """执行一轮 DataOps 校验（DDL + DML 各一次），汇总结果。

    本函数自身不负责 retry 编排 —— retry 由上层 validate_deliverables 控制：
    本工具每被调用一次，对每条 SQL 的 dataops wrapper 都从 attempt=1 开始。

    Returns:
        {
            "passed": bool,        # True 表示两 SQL 都通过/跳过
            "error": str,          # 失败原因（首条失败即可）
            "warning": str,        # DataOps 自己的警告
            "log_summary": str,    # 日志分析摘要（供前端展示）
        }
    """
    from dataagent.actions.tools.local_tool.dataops_validate_tool import (
        dataops_validate_sql_with_log_analysis,
    )

    summary: dict = {
        "passed": True,
        "error": "",
        "warning": "",
        "log_summary": "",
    }

    # 顺序：DDL 先校验；如果 DDL 失败 → 跳过 DML（DDL 是 DML 的前置）
    sql_jobs: list[tuple[str, Path]] = []
    if ddl_path is not None and not _is_empty(ddl_path):
        sql_jobs.append(("ddl", ddl_path))
    if dml_path is not None and not _is_empty(dml_path):
        sql_jobs.append(("dml", dml_path))

    if not sql_jobs:
        # 没有 SQL 文件可校验：理论上静态校验不会让代码走到这里，留作防御
        return summary

    # ------------------------------------------------------------------
    # DDL 重跑时 adhoctemp 临时表可能已被上次失败的 CREATE TABLE 创建，
    # 直接重跑会报 "Table already exists"。
    #
    # 预清理由 dataops_validate_sql 内部的 _pre_drop_for_create(联调) /
    # _pre_drop_for_create_prod(生产) 负责 —— 那里会同步等 DROP 真正执行完
    # 再提交 CREATE，确保 CREATE 之前同名表已消失，且不会在 CREATE 之后
    # 把刚建好的表误删。
    # ------------------------------------------------------------------

    # 对每条 SQL 各跑一次 wrapper；wrapper 内部会按 attempt 维护历史。
    # 因为每次 validate_deliverables 都从 attempt=1 开始，prior_attempts
    # 这里也总是空列表。
    for kind, path in sql_jobs:
        # 直接传文件路径,避免把整段 SQL 重复塞进 chat history;
        # 工具侧(_resolve_sql_input)会优先用 sql_file_path 读 SQL。
        try:
            # dataops_validate_sql_with_log_analysis 是 async coro;
            # validate_deliverables 是 sync tool,正常情况下用 asyncio.run 创建临时 loop。
            # 但若上游恰好在 running loop 里调用本工具,asyncio.run 会 RuntimeError,
            # 这里做兜底:有 running loop 时改用 thread 隔离 + asyncio.run。
            try:
                asyncio.get_running_loop()
                in_running_loop = True
            except RuntimeError:
                in_running_loop = False

            if in_running_loop:
                import concurrent.futures as _cf

                with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                    result = _pool.submit(
                        asyncio.run,
                        dataops_validate_sql_with_log_analysis(
                            sql_file_path=path,
                            attempt=1,
                            prior_attempts=[],
                            _tool_context=_tool_context,
                        ),
                    ).result()
            else:
                result = asyncio.run(
                    dataops_validate_sql_with_log_analysis(
                        sql_file_path=path,
                        attempt=1,
                        prior_attempts=[],
                        _tool_context=_tool_context,
                    )
                )
        except Exception as exc:
            logger.exception(f"[validate_deliverables] dataops call crashed kind={kind}")
            result = {"passed": False, "error": f"dataops wrapper crashed: {exc}"}

        # 记录到 state 里（跨 validate_deliverables 调用累计）
        state["dataops_attempts"].append(
            {
                "kind": kind,
                "sql_path": str(path),
                "passed": bool(result.get("passed")),
                "skipped": bool(result.get("skipped")),
                "error": (result.get("error") or "")[:500],
                "job_id": result.get("job_id"),
                "log_summary": result.get("log_analysis_summary") or "",
                "attempts": result.get("attempts"),
                "failed_after_max_retries": bool(result.get("failed_after_max_retries")),
            }
        )

        # 短路：失败立即返回
        passed = bool(result.get("passed"))
        skipped = bool(result.get("skipped"))
        if not (passed or skipped):
            summary["passed"] = False
            summary["error"] = (
                result.get("error") or (result.get("log_analysis_summary") or "DataOps validation failed (no detail)")
            )[:800]
            summary["log_summary"] = result.get("log_analysis_summary") or ""
            return summary

    return summary


def _dump_validate_deliverables_artifacts(
    *,
    state: dict,
    workspace_dir: Path,
    ddl_path: Path | None,
    dml_path: Path | None,
    _tool_context: ToolExecutionContext,
) -> dict:
    """达到上限时把完整 retry 历史 dump 到 workspace，best-effort，不抛异常。

    产出：
      - validate_deliverables_report.json：完整结构化重试历史（含每次 dataops 摘要）
      - validate_deliverables_warning.md：人类可读的 best-effort 警告（含修复建议）
    """
    out: dict = {"report_path": None, "warning_path": None}
    try:
        workspace_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning(f"[validate_deliverables] cannot create workspace {workspace_dir}: {exc}")
        return out

    report_path = workspace_dir / _DELIVERY_DELIVERABLES_REPORT_FILENAME
    warning_path = workspace_dir / _DELIVERY_DELIVERABLES_WARNING_FILENAME

    ddl_excerpt = ""
    if ddl_path is not None and ddl_path.exists():
        ddl_excerpt = ddl_path.read_text(encoding="utf-8")[:500]
    dml_excerpt = ""
    if dml_path is not None and dml_path.exists():
        dml_excerpt = dml_path.read_text(encoding="utf-8")[:500]

    report = {
        "validation_status": "failed_after_max_retries",
        "max_attempts": MAX_VALIDATE_DELIVERABLES_ATTEMPTS,
        "attempts_count": state.get("attempts", 0),
        "last_static_err": state.get("last_static_err", ""),
        "last_dataops_err": state.get("last_dataops_err", ""),
        "last_dataops_log_summary": state.get("last_dataops_log_summary", ""),
        "dataops_attempts": state.get("dataops_attempts", []),
        "ddl_excerpt": ddl_excerpt,
        "dml_excerpt": dml_excerpt,
        "generated_at": time.time(),
    }
    try:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        out["report_path"] = str(report_path)
    except Exception as exc:
        logger.warning(f"[validate_deliverables] failed to write report {report_path}: {exc}")

    # 人类可读警告
    attempt_lines: list[str] = []
    for entry in state.get("dataops_attempts", []):
        kind = entry.get("kind", "?")
        passed = entry.get("passed")
        skipped = entry.get("skipped")
        err = (entry.get("error") or "")[:200]
        log_summary = entry.get("log_summary") or ""
        verdict = "passed" if passed else ("skipped" if skipped else "failed")
        attempt_lines.append(f"- {kind.upper()} attempt [{verdict}]: {err}")
        if log_summary:
            attempt_lines.append(f"  - 日志分析: {log_summary[:200]}")

    warning_body = (
        f"# {_DELIVERY_DELIVERABLES_WARNING_HEADER.format(n=MAX_VALIDATE_DELIVERABLES_ATTEMPTS)}\n\n"
        f"## 失败摘要\n\n"
        + ("\n".join(attempt_lines) if attempt_lines else "(无 DataOps 数据)")
        + "\n\n## 详细报告\n\n"
        f"见 `{report_path.name}`（含本次会话 validate_deliverables 累计的 attempts、"
        f"dataops_attempts 历史、最后一次 DDL/DML 摘要）。\n"
        f"下游使用本批 SQL 前必须人工复核。\n"
    )
    try:
        warning_path.write_text(warning_body, encoding="utf-8")
        out["warning_path"] = str(warning_path)
    except Exception as exc:
        logger.warning(f"[validate_deliverables] failed to write warning {warning_path}: {exc}")

    return out


def _validate_deliverables(
    workspace_dir: Path,
    user_query: str = "",
    include_suggestions: bool = False,
    _tool_context: ToolExecutionContext | None = None,
) -> tuple[bool, str, str]:
    """校验最终产物是否存在且符合规范。"""
    errors = []
    warnings = []

    DDL, DML = _locate_sql(workspace=workspace_dir)
    if DDL is None:
        errors.append("Missing create_*.sql in workspace.")
    elif _is_empty(DDL):
        errors.append(f"File {str(DDL)} is empty.")
    else:
        ddl_errors = _validate_ddl_comprehensive(DDL)
        errors.extend(ddl_errors)

    if DML is None:
        errors.append("Missing insert_*.sql in workspace.")
    elif _is_empty(DML):
        errors.append(f"File {str(DML)} is empty.")
    else:
        _validate_dml(DML, errors)

    # LLM 质量门禁校验（仅在 DDL 和 DML 都存在且非空时调用）
    if DDL is not None and DML is not None and not _is_empty(DDL) and not _is_empty(DML):
        _validate_deliverables_quality_gate(DDL, DML, warnings, user_query, include_suggestions, _tool_context)

    err_msg = "Deliverables checks fail. Errors:\n- " + "\n- ".join(errors) if errors else ""
    warn_msg = "Deliverables checks have suggestions:\n- " + "\n- ".join(warnings) if warnings else ""

    return (len(errors) == 0, err_msg, warn_msg)


def _run_quality_gate_stream(llm: ChatModel, prompts: list[dict[str, str]]) -> str:
    """Bridge the synchronous tool to streaming while retaining tracing and model context."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(collect_llm_stream(llm, prompts))

    context = copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, asyncio.run, collect_llm_stream(llm, prompts)).result()


def _validate_deliverables_quality_gate(
    ddl_file: Path | None,
    dml_file: Path | None,
    warnings: list[str],
    user_query: str = "",
    include_suggestions: bool = False,
    _tool_context: ToolExecutionContext | None = None,
) -> None:
    """
    通过 LLM 对 DDL/DML 进行质量门禁校验。

    检查内容：
    1. 8 类质量自检表（空值判断、表名/字段名正确性、条件完整性、稳定排序、JION类型、
       业务逻辑完整性、口径冻结一致性）
    2. GATE Check（GATE-1, GATE-2）- 这些必须通过
    3. 其他检查项（DDL 方言、分区列处理、字段数对齐等）
    4. 交付前检查项和验证。

    注意：此函数仅将结果追加到 warnings，不阻塞交付流程。
    """
    # 获取 workspace 根目录
    from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox

    guard = get_current_sandbox()
    workspace_root = guard.workspace_root
    if workspace_root is None:
        warnings.append("LLM质量校验跳过：无法获取 workspace_root")
        return

    workspace_root = Path(workspace_root)

    # 读取元数据文件
    metadata_parts = []
    schemair_file = workspace_root / "schema_schemair.md"
    if schemair_file.exists():
        metadata_parts.append(f"# Schema Metadata\n{schemair_file.read_text(encoding='utf-8')}")

    udf_basic_file = workspace_root / "schema_udf_basic.md"
    if udf_basic_file.exists():
        metadata_parts.append(f"# UDF Metadata\n{udf_basic_file.read_text(encoding='utf-8')}")

    metadata_content = "\n\n".join(metadata_parts)

    # 读取 DDL 和 DML 内容
    ddl_content = ddl_file.read_text(encoding="utf-8") if ddl_file and ddl_file.exists() else ""
    dml_content = dml_file.read_text(encoding="utf-8") if dml_file and dml_file.exists() else ""

    # 获取IR
    runtime = _tool_context.runtime
    nl2sql_detail = runtime.get_cache("nl2sql_detail", {})

    rendered_ir = get_ir_context(runtime) or "**DataTaskIR 功能未启用**"
    nl2sql_detail = nl2sql_detail if nl2sql_detail else "**NL2SQL详细意图理解未找到**"

    # 构建提示词并调用 LLM（添加重试机制：最多重试1次）
    system_prompt, user_prompt = _build_quality_gate_prompt(
        ddl_content, dml_content, metadata_content, user_query, rendered_ir, nl2sql_detail
    )
    llm = llm_manager.get_default_llm()
    for attempt in range(2):
        try:
            content = _run_quality_gate_stream(
                llm, [{"role": "user", "content": system_prompt}, {"role": "user", "content": user_prompt}]
            )
            raw_result = content.strip()
            _parse_quality_gate_result(raw_result, warnings, include_suggestions)
            break  # 解析成功，退出循环
        except json.JSONDecodeError:
            if attempt == 0:
                logger.warning("LLM 质量校验第1次解析失败，尝试重新推理...")
                warnings.append("【LLM质量校验】JSON解析失败，重新推理...")
                continue  # 重试
            warnings.append("【LLM质量校验】LLM 返回无效 JSON，跳过详细检查")
        except Exception as e:
            logger.warning(f"LLM 质量校验执行异常: {e}")
            warnings.append(f"【LLM质量校验】执行异常: {str(e)}")
            break


def _is_empty(file_path: Path) -> bool:
    """校验文件是否为空"""
    content = file_path.read_text(encoding="utf-8")
    return len(content.strip()) == 0


def _extract_columns_block(content: str) -> str | None:
    """
    使用深度感知解析提取 CREATE TABLE 字段列表块。
    返回字段列表字符串（不包含包裹的括号），解析失败返回 None。
    """
    start_match = re.search(r"CREATE\s+EXTERNAL\s+TABLE", content, re.IGNORECASE)
    if not start_match:
        return None

    paren_start = content.find("(", start_match.end())
    if paren_start == -1:
        return None

    depth = 0
    for i in range(paren_start, len(content)):
        char = content[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return content[paren_start + 1 : i]

    return None


def _validate_dml(file_path: Path, errors: list) -> None:
    """
    综合校验DML各项规范，直接 append 错误到 errors 列表。
    Args:
        file_path: DML 文件路径
        errors: 错误列表，直接追加错误消息
    """
    # INSERT OVERWRITE PARTITION 校验
    valid, err_msg, err_line = _validate_insert_overwrite_partition(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # LATERAL VIEW + JOIN 校验
    valid, err_msg, err_line = _validate_lateral_view_join(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # 调度变量格式校验
    valid, err_msg, err_line = _validate_scheduler_variable_single_line(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # 校验 DML 中是否使用了当前 Spark/FI SQL 环境不支持的函数。
    valid, err_msg, _ = _validate_unsupported_functions(file_path)
    if not valid:
        errors.append(f"DML校验失败: \n{err_msg}")

    # UNIX_TO_STR 校验
    valid, err_msg, err_line = _validate_no_unix_to_str(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # REGEXP_SPLIT 校验
    valid, err_msg, err_line = _validate_no_regexp_split(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # CAST(... AS TEXT) 校验
    valid, err_msg, err_line = _validate_no_text_in_cast(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # ARRAY_UNIQUE_AGG 校验
    valid, err_msg, err_line = _validate_no_array_unique_agg(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # QUALIFY 校验
    valid, err_msg, err_line = _validate_no_qualify(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # CONCAT_WS + COLLECT_LIST ORDER BY 校验
    valid, err_msg, err_line = _validate_no_concat_ws_collect_list_order_by(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # DateFormat UDF 参数数量校验
    valid, err_msg, err_line = _validate_dateformat_single_param(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # DateFormat UDF 必须携带 bicoredata. 前缀
    valid, err_msg, err_line = _validate_dateformat_prefix(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # ConcatWithRank UDF 前缀 + 聚合上下文校验
    valid, err_msg, err_line = _validate_concat_with_rank_udf(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # JOIN 必须显式指定类型（LEFT/INNER/FULL OUTER 等）
    join_errors = _validate_join_type_explicit(file_path)
    errors.extend(join_errors)

    # REGEXP_SPLIT 校验
    valid, err_msg, err_line = _validate_target_table_with_database(file_path)
    if not valid:
        errors.append(f"DML校验失败: 第{err_line}行 - {err_msg}")

    # CTE 列引用校验
    cte_errors = _validate_sql_cte_errors(file_path)
    errors.extend(cte_errors)

    # CTE 多余逗号校验
    comma_errors = _validate_cte_comma_errors(file_path)
    errors.extend(comma_errors)


def _validate_ddl_comprehensive(file_path: Path) -> list:
    """
    综合校验DDL各项规范，返回错误列表，格式为 "第X行 - 错误信息"
    """
    errors = []
    content = file_path.read_text(encoding="utf-8")
    content_upper = content.upper()
    pt_cols = []

    if not content_upper.startswith("CREATE") and not content_upper.startswith("ALTER"):
        errors.append(
            f"DDL校验失败: 第{_line_no(content, 0)}行 - DDL文件必须以 CREATE 或 ALTER 开头，前面不能有空白或注释"
        )

    if not _validate_target_table_with_database(file_path):
        errors.append(f"DDL校验失败: 第{_line_no(content, 0)}行 - DDL 中目标表缺少库名，请使用 `库名.表名` 格式")

    if "CREATE EXTERNAL TABLE" not in content_upper:
        errors.append(f"DDL校验失败: 第{_line_no(content, 0)}行 - DDL缺少 CREATE EXTERNAL TABLE")

    if "IF NOT EXISTS" not in content_upper:
        errors.append(f"DDL校验失败: 第{_line_no(content, 0)}行 - DDL缺少 IF NOT EXISTS")

    if "STORED AS ORC" not in content_upper:
        errors.append(f"DDL校验失败: 第{_line_no(content, 0)}行 - DDL缺少 STORED AS ORC")

    if "PARTITIONED BY" not in content_upper:
        errors.append(f"DDL校验失败: 第{_line_no(content, 0)}行 - DDL缺少 PARTITIONED BY 子句")
    else:
        partition_match = re.search(r"PARTITIONED BY\s*\(([^)]+)\)", content_upper)
        if partition_match:
            partition_cols = partition_match.group(1)
            pt_cols = re.findall(r"\b(PT_\w+)\s+STRING", partition_cols)
            if not pt_cols:
                errors.append(f"DDL校验失败: 第{_line_no(content, partition_match.start())}行 - 分区列必须以 pt_ 开头")
            for pt_col in pt_cols:
                col_pattern = re.search(
                    rf"{pt_col}\s+STRING\s+COMMENT\s+[^\']*\'[^\']*分区[^\']*\'", partition_cols, re.IGNORECASE
                )
                if not col_pattern:
                    errors.append(
                        f"DDL校验失败: 第{_line_no(content, partition_match.start())}行 - 分区列 {pt_col} 必须包含 COMMENT 'xxx分区'"
                    )

    if "PRIMARY KEY" in content_upper:
        match = re.search(r"\bPRIMARY KEY\b", content_upper, re.IGNORECASE)
        if match:
            errors.append(
                f"DDL校验失败: 第{_line_no(content, match.start())}行 - DDL禁止使用 PRIMARY KEY（SQLite语法）"
            )

    columns_block = _extract_columns_block(content)
    if columns_block is not None:
        for pt_col in pt_cols:
            if re.search(rf"\b{pt_col}\b", columns_block.upper()):
                match = re.search(rf"\b{pt_col}\b", columns_block.upper())
                col_pos = match.start()
                full_pos = content.find(columns_block) + col_pos
                errors.append(f"DDL校验失败: 第{_line_no(content, full_pos)}行 - 分区列{pt_col}不应出现在字段列表中")

        field_type_errors = _validate_field_types(columns_block, content)
        errors.extend(field_type_errors)

        field_comment_errors = _validate_field_comments(columns_block)
        errors.extend(field_comment_errors)

        naming_errors = _validate_ddl_naming(content, columns_block)
        errors.extend(naming_errors)
    else:
        errors.append(f"DDL校验失败: 第{_line_no(content, 0)}行 - DDL解析失败：无法提取字段列表")

    if not re.search(r'\)\s*COMMENT\s+[\'"]', content, re.IGNORECASE):
        errors.append(f"DDL校验失败: 第{_line_no(content, 0)}行 - DDL缺少表级 COMMENT")

    return errors


def _validate_field_types(columns_block: str, full_content: str) -> list:
    """
    校验字段类型白名单
    - 允许: STRING, TINYINT, SMALLINT, INT, BIGINT, DECIMAL(precision,scale)
    - 禁止: DOUBLE, FLOAT, BOOLEAN, DATE, TIMESTAMP, VARCHAR, CHAR, ARRAY, MAP, STRUCT
    """
    errors = []
    content_upper = columns_block.upper()
    block_offset = full_content.find(columns_block)

    forbidden_types = ["DOUBLE", "FLOAT", "BOOLEAN", "DATE", "TIMESTAMP", "VARCHAR", "CHAR", "ARRAY", "MAP", "STRUCT"]
    for ftype in forbidden_types:
        match = re.search(rf"\b{ftype}\b", content_upper)
        if match:
            col_pos = match.start()
            full_pos = block_offset + columns_block.find(ftype, col_pos)
            errors.append(f"DDL校验失败: 第{_line_no(full_content, full_pos)}行 - DDL禁止使用 {ftype} 类型")

    decimal_matches = list(re.finditer(r"\bDECIMAL\b", content_upper))
    decimal_with_precision = re.findall(r"DECIMAL\s*\(\s*\d+\s*,\s*\d+\s*\)", content_upper)
    if decimal_matches and len(decimal_matches) != len(decimal_with_precision):
        dec_pos = decimal_matches[0].start()
        full_pos = block_offset + columns_block.find("DECIMAL", dec_pos)
        errors.append(
            f"DDL校验失败: 第{_line_no(full_content, full_pos)}行 - DECIMAL类型必须显式声明精度与小数位，如 DECIMAL(18,6)"
        )

    return errors


def _validate_field_comments(columns_block: str) -> list:
    """
    校验每个字段都有 COMMENT
    """
    errors = []
    lines = columns_block.split("\n")

    field_count = 0
    comment_count = 0

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if re.search(r"\b(STRING|TINYINT|SMALLINT|INT|BIGINT|DECIMAL)\b", stripped, re.IGNORECASE):
            field_count += 1
            if "COMMENT" in stripped.upper():
                comment_count += 1

    if field_count > 0 and comment_count < field_count:
        errors.append(
            f"DDL校验失败: 第1行 - DDL有{field_count}个字段，但只有{comment_count}个字段有COMMENT（每个字段都需要COMMENT）"
        )

    return errors


def _validate_ddl_naming(ddl_content: str, columns_block: str) -> list:
    """
    校验DDL命名规范：
    1. 表名只能包含小写字母、数字、下划线
    2. 字段名只能包含小写字母、数字、下划线
    3. COMMENT不能包含 *x✖✖️×@ 字符
    4. 表名前缀必须是ods/dwd/dws/dim/ads
    """
    errors = []

    full_table_match = re.search(
        r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:(\w+)\.)?(\w+)", ddl_content, re.IGNORECASE
    )
    if full_table_match:
        table_name = full_table_match.group(2)
        if table_name and not re.match(r"^[a-z0-9_]+$", table_name):
            invalid_chars = set(re.findall(r"[^a-z0-9_]", table_name))
            errors.append(
                f"DDL校验失败：第{_line_no(ddl_content, full_table_match.start(2))}行 - 表名 '{table_name}' "
                f"只能包含小写字母、数字、下划线，不应包含: {invalid_chars}"
            )
        if table_name and not re.match(r"^(ods|dwd|dws|dim|ads)", table_name):
            errors.append(
                f"DDL校验失败：第{_line_no(ddl_content, full_table_match.start(2))}行 - 表名 '{table_name}' "
                f"前缀必须是ods、dwd、dws、dim、ads"
            )

    forbidden_comment_chars = re.compile(r"[*x✖✖️×@]")

    field_comment_pattern = r"(\w+)\s+\w+(?:\([^)]+\))?\s*COMMENT\s+['\"]([^'\"]+)['\"]"
    field_comments = re.findall(field_comment_pattern, columns_block, re.IGNORECASE)

    for field_name, field_comment in field_comments:
        if field_comment:
            forbidden_found = forbidden_comment_chars.findall(field_comment)
            if forbidden_found:
                field_name_match = re.search(rf"\b{re.escape(field_name)}\b", columns_block)
                if field_name_match:
                    block_offset = ddl_content.find(columns_block)
                    line_no = _line_no(ddl_content, block_offset + field_name_match.start())
                    chars_str = "、".join(forbidden_found)
                    errors.append(
                        f"DDL校验失败：第{line_no}行 - 字段 '{field_name}' 的COMMENT不能包含 `{chars_str}` 字符"
                    )

    table_comment_match = re.search(r"\)\s*COMMENT\s+['\"]([^'\"]+)['\"]", ddl_content, re.IGNORECASE | re.DOTALL)
    if table_comment_match:
        table_comment = table_comment_match.group(1)
        forbidden_found = forbidden_comment_chars.findall(table_comment)
        if forbidden_found:
            chars_str = "、".join(forbidden_found)
            errors.append(
                f"DDL校验失败：第{_line_no(ddl_content, table_comment_match.start())}行 - "
                f"表级COMMENT不能包含 `{chars_str}` 字符"
            )

    field_line_pattern = r"^\s*,?\s*(\S+)\s+\w+(?:\(\d+(?:,\s*\d+)?\))?"
    field_names = re.findall(field_line_pattern, columns_block, re.IGNORECASE | re.MULTILINE)

    for field_name in set(field_names):
        if not re.match(r"^[a-z0-9_]+$", field_name):
            invalid_chars = set(re.findall(r"[^a-z0-9_]", field_name))
            block_offset = ddl_content.find(columns_block)
            full_pos = block_offset + columns_block.find(field_name)
            line_no = _line_no(ddl_content, full_pos)
            errors.append(
                f"DDL校验失败：第{line_no}行 - 字段名 '{field_name}' 只能包含小写字母、数字、下划线，不应包含: {invalid_chars}"
            )

    return errors


def _validate_target_table_with_database(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DDL/DML 中目标表是否缺少库名（必须使用 ``库名.表名``）。

    覆盖范围：

    * DDL: ``CREATE TABLE [IF NOT EXISTS] <目标>``
    * DML: ``INSERT OVERWRITE TABLE <目标>`` / ``INSERT INTO TABLE <目标>``

    当检测到目标表没有带库名（不含 ``.``）时返回错误。
    """
    content = file_path.read_text(encoding="utf-8")

    # 去掉 SQL 注释行（行内 -- 之后的内容），避免误判注释里的 CREATE/INSERT
    lines = content.split("\n")
    stripped_lines = []
    for line in lines:
        idx = line.find("--")
        if idx >= 0:
            stripped_lines.append(line[:idx])
        else:
            stripped_lines.append(line)
    content_no_comment = "\n".join(stripped_lines)
    content_upper = content_no_comment.upper()

    # 标识符允许：字母/数字/下划线，可整体被反引号/双引号/中括号包裹
    ident = r"`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[A-Za-z_]\w*"
    schema_group = rf"(?:({ident})\.)?"
    table_group = rf"({ident})"
    target_group = schema_group + table_group

    # DDL: CREATE [EXTERNAL] TABLE [IF NOT EXISTS] <target>
    ddl_pattern = re.compile(
        rf"\bCREATE\s+(?:EXTERNAL\s+)?TABLE\b(?:\s+IF\s+NOT\s+EXISTS)?\s+{target_group}",
        re.IGNORECASE,
    )

    # DML: INSERT OVERWRITE|INTO [TABLE] <target>
    dml_pattern = re.compile(
        rf"\bINSERT\s+(?:OVERWRITE|INTO)\b(?:\s+TABLE)?\s+{target_group}",
        re.IGNORECASE,
    )

    for pattern, kind in (
        (ddl_pattern, "DDL"),
        (dml_pattern, "DML"),
    ):
        for match in pattern.finditer(content_upper):
            schema = match.group(1)
            # 从原始 content 中按匹配偏移切片回小写原文（避免大写转换影响错误信息）
            start_in_original = match.start(2)
            end_in_original = match.end(2)
            table_original = content[start_in_original:end_in_original]
            if schema:
                # 已带库名
                continue
            line = _line_no(content, match.start(2))
            return (
                False,
                f"{kind}校验失败: 第{line}行 - 目标表 `{table_original}` 缺少库名，请使用 `库名.{table_original}` 格式",
                line,
            )

    return True, "", 0


def _validate_insert_overwrite_partition(file_path: Path) -> tuple[bool, str, int]:
    """
    校验DML是否满足：
    1. 必须使用 INSERT OVERWRITE + PARTITION
    2. 禁止 INSERT INTO / INSERT OR REPLACE
    3. WITH/CTE 必须位于 INSERT OVERWRITE 之前
    """
    content = file_path.read_text(encoding="utf-8")
    content_upper = content.upper()

    forbidden_patterns = [
        ("INSERT INTO", "禁止使用 INSERT INTO，请使用 INSERT OVERWRITE"),
        ("INSERT OR REPLACE", "禁止使用 INSERT OR REPLACE"),
    ]
    for pattern, msg in forbidden_patterns:
        match = re.search(pattern, content_upper, re.IGNORECASE)
        if match:
            return False, msg, _line_no(content, match.start())

    match = re.search(r"\bINSERT\s+OVERWRITE\b", content_upper, re.IGNORECASE)
    if not match:
        return False, "DML校验失败: 缺少 INSERT OVERWRITE", _line_no(content, 0)
    insert_line = _line_no(content, match.start())

    pattern = r"INSERT\s+OVERWRITE\s+.*?PARTITION\s*\(\s*PT_\w+\s*="
    if not re.search(pattern, content_upper, re.DOTALL):
        return (
            False,
            "DML校验失败: INSERT OVERWRITE 必须包含以 pt_ 开头的分区列的分区 PARTITION (pt_xxx=...)",
            insert_line,
        )

    with_matches = list(re.finditer(r"\bWITH\b", content_upper))
    insert_matches = list(re.finditer(r"\bINSERT\s+OVERWRITE\b", content_upper))

    if with_matches and insert_matches:
        first_with_pos = with_matches[0].start()
        first_insert_pos = insert_matches[0].start()
        if first_insert_pos < first_with_pos:
            return (
                False,
                "DML校验失败: WITH/CTE 必须位于 INSERT OVERWRITE 之前。"
                "当 INSERT 语句使用 WITH/CTE 时，无论 SparkSQL 使用什么版本，"
                "都要保证 WITH/CTE 必须位于 INSERT OVERWRITE 之前，"
                "正确示例（correct syntax）是 "
                "`WITH ... INSERT OVERWRITE ... PARTITION ... SELECT ...`。",
                insert_line,
            )

    return True, "", 0


def _validate_lateral_view_join(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 LATERAL VIEW 的使用方式。

    禁止：

        FROM table
        LATERAL VIEW ...
        LEFT JOIN dim ...

    推荐：

        FROM (
            SELECT ...
            FROM table
            LATERAL VIEW ...
        ) explode_t
        LEFT JOIN dim ...
    """

    content = file_path.read_text(encoding="utf-8")
    upper = content.upper()

    lateral_iter = re.finditer(r"\bLATERAL\s+VIEW\b", upper)

    join_pattern = re.compile(
        r"\b(?:LEFT|RIGHT|INNER|FULL|CROSS)?\s*JOIN\b",
        re.IGNORECASE,
    )

    for lateral in lateral_iter:
        join_match = join_pattern.search(upper, lateral.end())

        if join_match is None:
            continue

        between = content[lateral.end() : join_match.start()]

        # 判断 JOIN 前是否已经结束了子查询
        #
        # 允许：
        #
        #   ) t
        #   LEFT JOIN
        #
        # 不能简单用 r"\)\s*\w+" 判断，因为 LATERAL VIEW 的 UDTF 调用
        # 也包含 ) + 别名，如 POSEXPLODE(...) e AS pos, app_id
        # 这里 ) e 是函数的右括号 + 表别名，不是子查询关闭。
        #
        # 区分方法：UDTF 的 ) alias 后紧跟 AS + 列别名；
        # 子查询关闭的 ) alias 后紧跟 JOIN/WHERE/空白，不跟 AS。

        subquery_closed = False
        paren_alias_pattern = re.compile(
            r"\)\s*([A-Za-z_]\w*)\s+(AS\b)?",
            re.IGNORECASE,
        )
        paren_alias_matches = list(paren_alias_pattern.finditer(between))
        if paren_alias_matches:
            last_match = paren_alias_matches[-1]
            # 最后一个 ) alias 后没有 AS → 子查询关闭
            if not last_match.group(2):
                subquery_closed = True

        if subquery_closed:
            continue

        return (
            False,
            (
                "检测到 LATERAL VIEW 后直接使用 JOIN。"
                "请先将包含 LATERAL VIEW 的查询封装成子查询：\n\n"
                "FROM (\n"
                "    SELECT ...\n"
                "    FROM table\n"
                "    LATERAL VIEW ...\n"
                ") alias\n"
                "LEFT JOIN dim ..."
            ),
            _line_no(content, lateral.start()),
        )

    return True, "", 0


def _validate_scheduler_variable_single_line(file_path: Path) -> tuple[bool, str, int]:
    """
    校验调度变量 ${...} 必须写在同一行。

    合法：
        ${date}
        ${start_time,-30,yyyyMMdd}
        '${start_time,-30,yyyyMMdd}'

    非法：
        ${start_time,
          -30,
          yyyyMMdd}
    """

    content = file_path.read_text(encoding="utf-8")

    # 匹配所有 ${...}
    pattern = re.compile(r"\$\{.*?\}", re.DOTALL)

    for match in pattern.finditer(content):
        variable = match.group(0)

        # 内部出现换行
        if "\n" in variable or "\r" in variable:
            return (
                False,
                (f"调度变量 ${{...}} 必须位于同一行，不允许换行。检测到非法变量：{variable!r}"),
                _line_no(content, match.start()),
            )

    return True, "", 0


def _validate_no_unix_to_str(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DML 中禁止使用 UNIX_TO_STR。

    Spark SQL 标准函数为 from_unixtime。
    """

    content = file_path.read_text(encoding="utf-8")

    match = re.search(
        r"\bUNIX_TO_STR\s*\(",
        content,
        re.IGNORECASE,
    )

    if match:
        return (
            False,
            "检测到 UNIX_TO_STR()。请使用标准函数 from_unixtime() 替代。",
            _line_no(content, match.start()),
        )

    return True, "", 0


def _validate_no_text_in_cast(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DML 中禁止在 CAST 中使用 TEXT 类型。

    Spark SQL 应使用 STRING 类型。
    """

    content = file_path.read_text(encoding="utf-8")

    # 匹配 CAST(... AS TEXT) 或类似模式
    match = re.search(
        r"\bCAST\s*\([^)]*\bAS\s+TEXT\b",
        content,
        re.IGNORECASE,
    )

    if match:
        return (
            False,
            "检测到 CAST(... AS TEXT)。TEXT 不是有效的 Spark SQL 类型，请使用 STRING。",
            _line_no(content, match.start()),
        )

    return True, "", 0


def _validate_no_array_unique_agg(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DML 中禁止使用 ARRAY_UNIQUE_AGG。

    Spark SQL 应使用 COLLECT_SET 函数进行唯一值收集。
    """

    content = file_path.read_text(encoding="utf-8")

    match = re.search(
        r"\bARRAY_UNIQUE_AGG\s*\(",
        content,
        re.IGNORECASE,
    )

    if match:
        return (
            False,
            "检测到 ARRAY_UNIQUE_AGG()。请使用 COLLECT_SET() 替代。",
            _line_no(content, match.start()),
        )

    return True, "", 0


def _validate_dateformat_single_param(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DML 中 bicoredata.DateFormat UDF 只能传入一个参数。

    bicoredata.DateFormat 做日期格式转换时只能传入日期字符串一个参数，
    传入多个参数（如格式字符串）属于误用。
    """

    content = file_path.read_text(encoding="utf-8")

    # 匹配 bicoredata.DateFormat( 或 BICOREDATA.DATEFORMAT(
    for match in re.finditer(
        r"\bbicoredata\.DateFormat\s*\(",
        content,
        re.IGNORECASE,
    ):
        # 找到 DateFormat 后的左括号位置，解析参数数量
        paren_start = match.end() - 1
        param_count = _count_function_params(content, paren_start)

        if param_count > 1:
            return (
                False,
                "使用平台UDF函数 `bicoredata.DateFormat` 做日期格式转换时，只能传入日期字符串一个参数。",
                _line_no(content, match.start()),
            )

    return True, "", 0


def _is_in_comment(content: str, pos: int) -> bool:
    """
    判断 content 中位置 pos 是否处于注释内部。

    支持 SQL 的两种注释形式：
    - 单行注释：-- ...
    - 多行注释：/* ... */
    """
    # 检查是否在 /* ... */ 多行注释内
    # 找到 pos 之前最后一个 /* ，再看它后面是否已有 */
    last_open = content.rfind("/*", 0, pos)
    if last_open >= 0:
        last_close = content.rfind("*/", last_open, pos)
        if last_close < 0:
            return True

    # 检查是否在 -- 单行注释内
    # 从 pos 向前找最近的一个换行符，再从换行符后找是否有 --
    last_newline = content.rfind("\n", 0, pos)
    line_start = last_newline + 1
    line_before_pos = content[line_start:pos]
    if "--" in line_before_pos:
        # 还需排除 -- 出现在字符串字面量中的情况
        # 简化处理：统计 line_before_pos 中 -- 之前未闭合的引号数量
        dash_pos = line_before_pos.index("--")
        segment = line_before_pos[:dash_pos]
        single_count = segment.count("'") - segment.count("\\'")
        if single_count % 2 == 0:
            # 偶数个未转义引号，说明 -- 不在字符串内
            return True

    return False


def _validate_dateformat_prefix(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DML 中使用 DateFormat UDF 时必须携带 bicoredata. 前缀。

    DateFormat 是平台自定义 UDF，必须以 bicoredata.DateFormat 的完整限定名调用，
    不能仅写 DateFormat()。
    """

    content = file_path.read_text(encoding="utf-8")

    # 查找所有 DateFormat( 调用，排除前面已有 bicoredata. 的情况
    for match in re.finditer(r"\bDateFormat\s*\(", content, re.IGNORECASE):
        # 跳过注释中的匹配
        if _is_in_comment(content, match.start()):
            continue

        # 检查前面是否有 bicoredata.
        before = content[: match.start()]
        # 匹配 bicoredata. 紧贴在 DateFormat 之前（允许 bicoredata 与点号之间及点号与 DateFormat 之间有空白）
        if re.search(r"\bbicoredata\s*\.\s*$", before, re.IGNORECASE):
            continue

        return (
            False,
            "使用 DateFormat UDF 时必须携带 `bicoredata.` 前缀，请使用 `bicoredata.DateFormat()` 而非 `DateFormat()`。",
            _line_no(content, match.start()),
        )

    return True, "", 0


def _validate_concat_with_rank_udf(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DML 中使用 ConcatWithRank UDF 的规范（不区分大小写）：

    1. 必须携带 bicoredata. 前缀调用，不能仅写 ConcatWithRank()。
    2. ConcatWithRank 是聚合函数，必须在含 GROUP BY 的聚合查询中使用
       （HAVING / 窗口函数 OVER 等聚合上下文同样视为合法）。
    """
    content = file_path.read_text(encoding="utf-8")

    # 检查1：bicoredata. 前缀
    for match in re.finditer(r"\bConcatWithRank\s*\(", content, re.IGNORECASE):
        if _is_in_comment(content, match.start()):
            continue
        before = content[: match.start()]
        if re.search(r"\bbicoredata\s*\.\s*$", before, re.IGNORECASE):
            continue
        return (
            False,
            "使用 ConcatWithRank UDF 时必须携带 `bicoredata.` 前缀，"
            "请使用 `bicoredata.ConcatWithRank(value, rank_col, separator)` "
            "而非 `ConcatWithRank(value, rank_col, separator)`。",
            _line_no(content, match.start()),
        )

    # 检查2：聚合上下文（GROUP BY / HAVING / 窗口 OVER）
    try:
        tree = sqlglot.parse_one(content, read="spark")
    except Exception as e:
        logger.warning(f"sqlglot parse error in ConcatWithRank check: {e}\n")
        return True, "", 0

    for node in tree.walk():
        # ConcatWithRank 非 sqlglot 内置函数，带前缀或裸调用均解析为 Anonymous
        if not isinstance(node, exp.Anonymous):
            continue
        func_name = node.this
        if not isinstance(func_name, str) or func_name.lower().split(".")[-1] != "concatwithrank":
            continue

        # 向上找最近的 SELECT 祖先，同时检测是否位于窗口函数内
        select_node = None
        in_window = False
        current = node.parent
        while current is not None:
            if isinstance(current, exp.Window):
                in_window = True
            if isinstance(current, exp.Select):
                select_node = current
                break
            current = current.parent

        if select_node is None:
            continue

        has_group = select_node.args.get("group") is not None
        has_having = select_node.args.get("having") is not None
        if has_group or has_having or in_window:
            continue

        # 定位行号：优先匹配完整调用 SQL，回退到函数名匹配；均跳过注释中的匹配
        node_pos = _locate_concat_with_rank_pos(node, content)
        if node_pos == -1:
            continue

        return (
            False,
            "疑似问题：`bicoredata.ConcatWithRank` 是聚合函数，必须与 GROUP BY 等聚合算子一起使用，"
            "请改为在含 GROUP BY 等聚合查询中调用。",
            _line_no(content, node_pos),
        )

    return True, "", 0


def _validate_join_type_explicit(file_path: Path) -> list[str]:
    """
    校验 DML 中 JOIN 是否显式指定了类型（LEFT / INNER / RIGHT / FULL OUTER / CROSS 等）。

    裸写 JOIN（即 INNER JOIN 的简写）在 Spark 中虽合法，但容易与 LEFT JOIN 混淆
    导致数据丢失或重复，因此要求必须显式写出 JOIN 类型。

    使用 sqlglot AST 解析：exp.Join 节点的 kind 和 side 均为 None 时即为裸 JOIN。
    """
    content = file_path.read_text(encoding="utf-8")

    try:
        tree = sqlglot.parse_one(content, read="spark")
    except Exception as e:
        logger.warning(f"sqlglot parse error in JOIN type check: {e}\n")
        return []

    errors = []
    for node in tree.walk():
        if not isinstance(node, exp.Join):
            continue
        kind = node.args.get("kind")
        side = node.args.get("side")
        if kind is None and side is None:
            # 裸 JOIN，在原始内容中定位行号
            line_no = _locate_join_line_no(node, content)
            errors.append(
                f"DML校验失败: 第{line_no}行 - "
                "禁止使用裸 JOIN，必须显式指定 JOIN 类型（LEFT JOIN "
                "/ FULL OUTER JOIN / CROSS JOIN 等）。"
            )

    return errors


def _locate_join_line_no(node: exp.Join, content: str) -> int:
    """
    在原始 content 中定位裸 JOIN 关键字的行号。

    优先使用 node.sql() 输出（如 "JOIN t2 ON ..."）在原始内容中不区分大小写搜索，
    跳过注释中的匹配；若未找到则回退到按关键字 "JOIN" 逐行搜索。
    """
    node_sql = node.sql()

    # 尝试用完整 JOIN 子句定位
    escaped = re.escape(node_sql.split(" ON ")[0] if " ON " in node_sql else node_sql)
    if escaped:
        for match in re.finditer(escaped, content, re.IGNORECASE):
            if _is_in_comment(content, match.start()):
                continue
            return _line_no(content, match.start())

    # 回退：逐行搜索独立的 JOIN 关键字
    for line_idx, line in enumerate(content.split("\n"), start=1):
        stripped = line.split("--")[0]  # 去掉行内注释
        if re.search(r"\bJOIN\b", stripped, re.IGNORECASE):
            # 检查前面是否有 LEFT/INNER/RIGHT/FULL/CROSS 等修饰词
            before = stripped[: stripped.upper().index("JOIN")]
            if not re.search(r"\b(LEFT|RIGHT|INNER|FULL|CROSS|OUTER)\s*$", before, re.IGNORECASE):
                return line_idx

    return 0


def _locate_concat_with_rank_pos(node: exp.Anonymous, content: str) -> int:
    """
    在原始 content 中定位 ConcatWithRank 调用的字符位置。

    sqlglot 生成 SQL 时可能将函数名规范化为大写，因此使用不区分大小写匹配；
    同时跳过注释中的匹配，避免注释内同名文本干扰行号定位。

    Returns:
        调用起始字符位置；未找到返回 -1
    """
    for match in re.finditer(re.escape(node.sql()), content, re.IGNORECASE):
        if not _is_in_comment(content, match.start()):
            return match.start()
    for match in re.finditer(r"\bConcatWithRank\s*\(", content, re.IGNORECASE):
        if not _is_in_comment(content, match.start()):
            return match.start()
    return -1


def _count_function_params(content: str, paren_pos: int) -> int:
    """
    计算函数调用中顶层参数的数量。

    Args:
        content: 完整文件内容
        paren_pos: 左括号 '(' 的位置

    Returns:
        顶层参数数量。0 表示空参数列表 ()。
    """
    depth = 1  # 已进入第一层括号
    i = paren_pos + 1
    param_count = 0
    has_content = False

    while i < len(content) and depth > 0:
        char = content[i]

        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                # 括号闭合前如果有内容，最后一个参数计数
                if has_content:
                    param_count += 1
                break
        elif char == "," and depth == 1:
            # 顶层逗号分隔参数
            param_count += 1
            has_content = False
        elif char in ("'", '"'):
            # 跳过字符串字面量
            has_content = True
            quote = char
            i += 1
            while i < len(content) and content[i] != quote:
                if content[i] == "\\":
                    i += 2  # 跳过转义字符
                else:
                    i += 1
            # i 现在指向结束引号，循环末尾会 i += 1
        elif not char.isspace():
            has_content = True

        i += 1

    return param_count


def _validate_no_regexp_split(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DML 中禁止使用 REGEXP_SPLIT。

    Spark SQL 标准函数为 SPLIT，REGEXP_SPLIT 不是标准函数。
    """

    content = file_path.read_text(encoding="utf-8")

    match = re.search(
        r"\bREGEXP_SPLIT\s*\(",
        content,
        re.IGNORECASE,
    )

    if match:
        return (
            False,
            "检测到 REGEXP_SPLIT()。请使用标准函数 SPLIT() 替代。",
            _line_no(content, match.start()),
        )

    return True, "", 0


def _validate_no_qualify(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DML 中禁止使用 QUALIFY 关键字。

    Spark SQL 3.1 不支持 QUALIFY 子句，应使用其它方式。
    """

    content = file_path.read_text(encoding="utf-8")

    # 匹配作为 SQL 子句的 QUALIFY 关键字（避免误判注释或字符串中的 QUALIFY）
    # 去掉 SQL 注释行，避免误判
    lines = content.split("\n")
    stripped_lines = []
    for line in lines:
        idx = line.find("--")
        if idx >= 0:
            stripped_lines.append(line[:idx])
        else:
            stripped_lines.append(line)
    content_no_comment = "\n".join(stripped_lines)

    match = re.search(
        r"\bQUALIFY\b",
        content_no_comment,
        re.IGNORECASE,
    )

    if match:
        return (
            False,
            "当前执行环境 Spark 3.1 不支持QUALIFY，请不要使用QUALIFY关键字。",
            _line_no(content, match.start()),
        )

    return True, "", 0


def _validate_unsupported_functions(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DML 中是否使用了当前 Spark/FI SQL 环境不支持的函数。
    """

    content = file_path.read_text(encoding="utf-8")

    unsupported_functions = {
        "TRY_CAST": "CAST",
        "ARRAY_AGG": "collect_list / collect_set",
    }

    errors = []
    first_line = 0

    for func, replacement in unsupported_functions.items():
        for match in re.finditer(
            rf"\b{re.escape(func)}\s*\(",
            content,
            re.IGNORECASE,
        ):
            line = _line_no(content, match.start())

            if first_line == 0:
                first_line = line

            errors.append(f"- 第{line}行：检测到 {func}()，当前执行环境SparkSQL 3.1不支持，请改用 {replacement}()。")

    if errors:
        return False, "\n".join(errors), first_line

    return True, "", 0


def _validate_no_concat_ws_collect_list_order_by(file_path: Path) -> tuple[bool, str, int]:
    """
    校验 DML 中禁止使用 CONCAT_WS(',', COLLECT_LIST(x ORDER BY ...)) 作为最终序列实现。

    Spark SQL 不支持在 COLLECT_LIST 的参数表达式后直接加 ORDER BY。
    """

    content = file_path.read_text(encoding="utf-8")

    # 查找所有 CONCAT_WS 调用
    concat_ws_pattern = re.compile(r"\bCONCAT_WS\s*\(", re.IGNORECASE)

    for concat_match in concat_ws_pattern.finditer(content):
        concat_start = concat_match.start()
        # 找到 CONCAT_WS 后的括号位置，开始解析参数
        paren_start = concat_match.end() - 1

        # 解析 CONCAT_WS 的参数，找到 COLLECT_LIST 部分
        collect_pos = _find_collect_list_in_concat_ws(content, paren_start)

        if collect_pos != -1:
            # 检查 COLLECT_LIST 后是否有 ORDER BY（支持嵌套括号匹配）
            after_collect = content[collect_pos:]
            order_by_match = re.search(
                r"COLLECT_LIST\s*\((?:[^()]|\([^()]*\))*\bORDER\s+BY\b", after_collect, re.IGNORECASE | re.DOTALL
            )
            if order_by_match:
                return (
                    False,
                    (
                        "检测到 CONCAT_WS 内部使用 COLLECT_LIST(x ORDER BY ...)。"
                        "当前执行环境禁止 DML 中使用 CONCAT_WS(',', COLLECT_LIST(x ORDER BY ...)) 作为最终序列实现的语法。\n\n"
                        "建议要求排序的序列特征必须先在子查询中用窗口函数排序，再拼接：\n"
                        "1. 用窗口函数排序生成排序号：\n"
                        "   ROW_NUMBER() OVER (PARTITION BY key ORDER BY sort_col DESC) AS rn\n\n"
                        "2. 在外层 WHERE 中筛选或限制数量后，再用 COLLECT_SET/COLLECT_LIST 拼接：\n"
                        "   CONCAT_WS(',', COLLECT_SET(x))"
                    ),
                    _line_no(content, concat_start),
                )

    return True, "", 0


def _find_collect_list_in_concat_ws(content: str, concat_ws_paren_pos: int) -> int:
    """
    在 CONCAT_WS( 之后找到 COLLECT_LIST 调用的位置。
    返回 COLLECT_LIST 的 'C' 位置，如果没找到返回 -1。
    """
    depth = 1  # 从 CONCAT_WS 后的第一个左括号开始
    i = concat_ws_paren_pos + 1
    comma_count = 0  # 记录逗号数量，找到第二个参数

    while i < len(content) and depth > 0:
        char = content[i]

        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        elif char == "," and depth == 1:
            comma_count += 1
            if comma_count == 1:
                # 跳过第一个参数（分隔符），继续解析第二个参数
                pass
        elif char in ("'", '"'):
            # 跳过字符串字面量
            quote = char
            i += 1
            while i < len(content) and content[i] != quote:
                if content[i] == "\\":
                    i += 2  # 跳过转义字符
                else:
                    i += 1
            i += 1
            continue

        # 在第二参数范围内查找 COLLECT_LIST
        if comma_count >= 1 and depth >= 1 and content[i : i + 12].upper() == "COLLECT_LIST":
            return i

        i += 1

    return -1


def _locate_sql(workspace: Path) -> tuple[Path | None, Path | None]:
    """定位sql文件"""
    DDL, DML = None, None
    if not os.path.exists(workspace):
        return DDL, DML

    for f in os.listdir(workspace):
        if f.startswith("create_") and f.endswith(".sql") and DDL is None:
            DDL = workspace / f
        if f.startswith("insert_") and f.endswith(".sql") and DML is None:
            DML = workspace / f

    return DDL, DML


def _is_alter_scenario(workspace: Path) -> bool:
    """检测工作区是否为 ALTER/ADD_COLUMN 场景（存在 alter_*.sql 文件）。"""
    if not os.path.exists(workspace):
        return False
    return any(f.startswith("alter_") and f.endswith(".sql") for f in os.listdir(workspace))


def _get_cte_columns_from_node(cte_node: exp.CTE) -> set[str] | None:
    """
    从 CTE 节点提取列名集合。
    如果 CTE SELECT 列表包含 *，返回 None（表示无法验证）。
    """
    inner = cte_node.this
    # UNION / INTERSECT / EXCEPT 的列名由第一个 SELECT 定义
    if isinstance(inner, (exp.Union, exp.Intersect, exp.Except)):
        inner = inner.find(exp.Select)
        if inner is None:
            return set()

    if not hasattr(inner, "expressions"):
        return set()

    columns = set()
    for item in inner.expressions:
        if isinstance(item, exp.Alias):
            columns.add(item.alias.lower())
        elif isinstance(item, exp.Column):
            columns.add(item.name.lower())
        elif isinstance(item, exp.Star):
            return None
    return columns


def _validate_cte_comma_errors(file_path: Path | None) -> list[str]:
    """
    检查 WITH 子句中 CTE 定义结尾是否存在多余的逗号。

    检查模式: ), 后紧跟 INSERT（中间可夹注释），说明逗号后无 CTE 定义
    """
    if file_path is None:
        return []

    content = file_path.read_text(encoding="utf-8").strip()
    if not content:
        return []

    errors = []
    for match in re.finditer(
        r"\)\s*,\s*(?:(?:--[^\n]*|/\*[\s\S]*?\*/)\s*)*INSERT\b",
        content,
        re.IGNORECASE,
    ):
        pos = match.start()
        errors.append(f"DML校验失败: 第{_line_no(content, pos)}行 - CTE定义后存在多余逗号，请删除逗号")

    return errors


def _validate_sql_cte_errors(file_path: Path | None) -> list[str]:
    """
    使用 sqlglot AST 分析检查 CTE 列引用错误。

    检查 CTE 定义中缺少外层 SELECT 引用的列。
    支持嵌套子查询内的 CTE，内层 CTE 可遮蔽外层同名 CTE。

    Returns:
        错误信息列表，格式为 "第X行 - CTE 'xxx' does not have column 'yyy'"
    """
    if file_path is None:
        return []

    content = file_path.read_text(encoding="utf-8")
    errors = []

    try:
        tree = sqlglot.parse_one(content, read="spark")
    except Exception as e:
        logger.warning(f"sqlglot parse error: {e}\n")
        return [f"DML校验失败: SQL语法错误，无法解析 — {e}"]

    # 主入口：开始验证
    try:
        # 处理主查询
        main_with = tree.find(exp.With)
        if main_with:
            visible_ctes = {}
            cte_nodes_ordered = []
            for cte_node in main_with.walk():
                if isinstance(cte_node, exp.CTE):
                    cte_name = cte_node.alias.lower()
                    cte_columns = _get_cte_columns_from_node(cte_node)
                    visible_ctes[cte_name] = cte_columns
                    cte_nodes_ordered.append(cte_node)

            # 逐个 CTE 独立校验：每个 CTE 的 SELECT 有自己的 FROM 来源
            for cte_node in cte_nodes_ordered:
                cte_select = cte_node.this
                if isinstance(cte_select, exp.Select):
                    # 计算 CTE 在原始 content 中的起始字符位置，用于精确定位行号
                    cte_name = cte_node.alias.lower()
                    scope_start = _find_cte_start_char(cte_name, content)
                    _process_select_for_cte_validation(cte_select, visible_ctes, errors, content, scope_start)

            # 校验主查询（CTE 之后的最终 INSERT SELECT）
            main_select = tree.find(exp.Select)
            if main_select:
                # 主查询起始位置为 WITH 关键字之后
                with_match = re.search(r"\bWITH\b", content, re.IGNORECASE)
                scope_start = with_match.start() if with_match else 0
                _process_select_for_cte_validation(main_select, visible_ctes, errors, content, scope_start)
    except Exception as e:
        logger.warning(f"CTE validation exception: {e}\n")
        errors.append(f"DML校验失败: CTE校验过程中发生异常 — {e}")

    return errors


def _find_cte_start_char(cte_name: str, content: str) -> int:
    """查找 CTE 名在原始 content 中的起始字符位置，用于行号定位"""
    # 匹配 CTE 定义模式: cte_name AS (
    pattern = rf"\b{re.escape(cte_name)}\s+AS\s*\("
    match = re.search(pattern, content, re.IGNORECASE)
    return match.start() if match else 0


def _build_alias_to_cte_mapping(select_node: exp.Select, visible_ctes: dict[str, set[str] | None]) -> dict[str, str]:
    """构建当前 SELECT 直接引用的别名到 CTE 的映射（FROM + JOIN，不递归子查询）"""
    alias_to_cte = {}
    from_clause = select_node.find(exp.From)
    if from_clause:
        for table_node in from_clause.find_all(exp.Table):
            table_name = table_node.name.lower()
            alias = table_node.alias.lower() if table_node.alias else table_name
            if table_name in visible_ctes:
                alias_to_cte[alias] = table_name
    for join_node in select_node.args.get("joins") or []:
        for table_node in join_node.find_all(exp.Table):
            table_name = table_node.name.lower()
            alias = table_node.alias.lower() if table_node.alias else table_name
            if table_name in visible_ctes:
                alias_to_cte[alias] = table_name
    return alias_to_cte


def _collect_lateral_view_columns(select_node: exp.Select) -> tuple[set[str], dict[str, set[str]]]:
    """收集当前 SELECT 中 LATERAL VIEW 的列信息。

    Returns:
        bare_columns: 所有 LATERAL VIEW 输出的裸列名集合（用于跳过裸列校验）
        alias_to_columns: LATERAL VIEW 别名到其输出列集合的映射（用于校验带前缀引用如 s.imei）
    """
    bare_columns: set[str] = set()
    alias_to_columns: dict[str, set[str]] = {}

    for lateral in select_node.find_all(exp.Lateral):
        alias_node = lateral.args.get("alias")
        explicit_cols: set[str] = set()
        lateral_alias: str | None = None

        if alias_node and hasattr(alias_node, "args"):
            col_nodes = alias_node.args.get("columns")
            if col_nodes:
                for c in col_nodes:
                    explicit_cols.add(c.name.lower() if hasattr(c, "name") else str(c).lower())
            this_ident = alias_node.args.get("this")
            if this_ident and hasattr(this_ident, "name"):
                lateral_alias = this_ident.name.lower()

        if explicit_cols:
            bare_columns.update(explicit_cols)
        else:
            # 无显式列别名时，根据 UDTF 类型推断默认输出列名
            inner_func = lateral.args.get("this")
            if isinstance(inner_func, exp.Posexplode):
                explicit_cols = {"pos", "col"}
            elif isinstance(inner_func, exp.Explode):
                explicit_cols = {"col"}
            bare_columns.update(explicit_cols)

        if lateral_alias and explicit_cols:
            alias_to_columns[lateral_alias] = explicit_cols

    return bare_columns, alias_to_columns


def _find_line_no(col_node: exp.Column, content: str, scope_start_char: int = 0) -> int:
    """根据 AST 节点在原始 content 中定位行号，仅在 scope_start_char 之后搜索"""
    col_sql = col_node.sql()
    col_name = col_node.name.lower() if col_node.name else ""
    escaped = re.escape(col_sql)
    for match in re.finditer(escaped, content):
        if match.start() >= scope_start_char:
            return content[: match.start()].count("\n") + 1
    # 回退：用列名搜索
    for match in re.finditer(re.escape(col_name), content):
        if match.start() >= scope_start_char:
            return content[: match.start()].count("\n") + 1
    return 0


def _check_single_column_ref(
    col_node: exp.Column,
    alias_to_cte: dict[str, str],
    visible_ctes: dict[str, set[str] | None],
    errors: list[str],
    content: str,
    scope_start_char: int = 0,
    lateral_alias_to_columns: dict[str, set[str]] | None = None,
) -> None:
    """检查单个列引用，支持无表别名前缀的裸列名"""
    col_table = col_node.table.lower() if col_node.table else None
    col_name = col_node.name.lower() if col_node.name else None

    if not col_name:
        return

    if col_table:
        # 有表别名前缀的情况
        if col_table not in alias_to_cte:
            # 检查是否为 LATERAL VIEW 别名
            if lateral_alias_to_columns and col_table in lateral_alias_to_columns:
                lateral_cols = lateral_alias_to_columns[col_table]
                if col_name not in lateral_cols:
                    line_no = _find_line_no(col_node, content, scope_start_char)
                    errors.append(
                        f"DML校验失败: 第{line_no}行 - LATERAL VIEW '{col_table}' does not have column '{col_name}' "
                        f"(available: {', '.join(sorted(lateral_cols))})"
                    )
            return
        cte_name = alias_to_cte[col_table]
        cte_columns = visible_ctes[cte_name]
        if cte_columns is not None and col_name not in cte_columns:
            line_no = _find_line_no(col_node, content, scope_start_char)
            errors.append(
                f"DML校验失败: 第{line_no}行 - CTE '{cte_name}' does not have column '{col_name}' "
                f"(referenced as '{col_table}.{col_name}')"
            )
    else:
        # 无表别名前缀的裸列名：在当前 SELECT 的 CTE 来源中查找
        cte_sources = list(alias_to_cte.values())
        if not cte_sources:
            return
        verifiable_ctes = [c for c in cte_sources if visible_ctes.get(c) is not None]
        if not verifiable_ctes:
            return
        if all(col_name not in visible_ctes[c] for c in verifiable_ctes):
            line_no = _find_line_no(col_node, content, scope_start_char)
            cte_list = ", ".join(verifiable_ctes)
            errors.append(f"DML校验失败: 第{line_no}行 - Column '{col_name}' not found in any CTE source ({cte_list})")


def _validate_select_cte_refs(
    select_node: exp.Select,
    visible_ctes: dict[str, set[str] | None],
    errors: list[str],
    content: str,
    scope_start_char: int = 0,
) -> None:
    """校验单个 SELECT 节点中的列引用（仅当前层级，不递归子查询）"""
    alias_to_cte = _build_alias_to_cte_mapping(select_node, visible_ctes)
    if not alias_to_cte:
        return

    # 收集 LATERAL VIEW 输出的裸列名及别名映射
    lateral_columns, lateral_alias_to_columns = _collect_lateral_view_columns(select_node)

    for col_node in select_node.find_all(exp.Column):
        # 跳过属于嵌套子查询的列引用，只校验当前 SELECT 层级
        if _is_inside_nested_subquery(col_node, select_node):
            continue
        # 跳过 LATERAL VIEW（如 POSEXPLODE）动态生成的裸列
        col_name = col_node.name.lower() if col_node.name else None
        if col_name and not col_node.table and col_name in lateral_columns:
            continue
        _check_single_column_ref(
            col_node,
            alias_to_cte,
            visible_ctes,
            errors,
            content,
            scope_start_char,
            lateral_alias_to_columns=lateral_alias_to_columns,
        )


def _collect_inner_ctes(
    select_node: exp.Select, inherited_ctes: dict[str, set[str] | None]
) -> dict[str, set[str] | None]:
    """收集当前 SELECT 节点内部定义的 CTE"""
    inner_ctes = inherited_ctes.copy()
    inner_with = select_node.find(exp.With)
    if not inner_with:
        return inner_ctes

    for cte_node in inner_with.walk():
        if isinstance(cte_node, exp.CTE):
            cte_name = cte_node.alias.lower()
            cte_columns = _get_cte_columns_from_node(cte_node)
            inner_ctes[cte_name] = cte_columns
    return inner_ctes


def _is_inside_nested_subquery(col_node: exp.Column, scope_select: exp.Select) -> bool:
    """判断列引用是否位于 scope_select 的嵌套子查询中（不属于当前层级）"""
    current = col_node.parent
    while current is not None:
        if isinstance(current, exp.Select) and current != scope_select:
            return True
        # 遇到 scope_select 本身则停止，说明属于当前层级
        if current == scope_select:
            return False
        current = current.parent
    return False


def _process_select_for_cte_validation(
    select_node: exp.Select,
    visible_ctes: dict[str, set[str] | None],
    errors: list[str],
    content: str,
    scope_start_char: int = 0,
) -> None:
    """处理 SELECT 节点中的列引用验证，递归进入子查询"""
    _validate_select_cte_refs(select_node, visible_ctes, errors, content, scope_start_char)

    # 递归进入子查询
    for subquery_node in select_node.find_all(exp.Subquery):
        inner_select = subquery_node.find(exp.Select)
        if inner_select:
            inner_ctes = _collect_inner_ctes(inner_select, visible_ctes)
            _process_select_for_cte_validation(inner_select, inner_ctes, errors, content, scope_start_char)


def _line_no(content: str, pos: int) -> int:
    """根据字符位置计算行号（1-indexed）"""
    return content[:pos].count("\n") + 1
