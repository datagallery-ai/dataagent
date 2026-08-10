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

"""Query fixtures and expected answers for the bio_lab performance e2e test."""

QUERY_SEQUENCES = {
    "create_experiment": {
        "query": "帮我创建BD55-1111抗体和XBB.1.5病毒的中和实验（使用huh-7细胞）",
        "feedback_responses": ["确认，请创建该中和实验"],
        "needs_feedback": True,
    },
    "find_antibody_neutralization": {
        "query": "查一下 帮我找出抗体 'BD-368' 能有效中和（IC50小于0.1）的所有假病毒名称",
        "needs_feedback": False,
    },
    "find_recent_experiment": {
        "query": "XBB.1.5和BD55-1111的中和实验，最近一次的实验编号是多少",
        "needs_feedback": False,
    },
    "count_cells": {
        "query": "有多少个不同类型的细胞，ID分别是什么",
        "needs_feedback": False,
    },
    "count_viruses": {
        "query": "有多少个不同类型的病毒，ID分别是什么",
        "needs_feedback": False,
    },
    "count_antibodies": {
        "query": "一共有多少个不同类型的抗体，ID是多少",
        "needs_feedback": False,
    },
    "ask_recent_experiment_id": {
        "query": "刚才创建的实验实验编号是多少",
        "needs_feedback": False,
    },
}

BAD_CASE_QUERY_SEQUENCES = {
    "TC09": {
        # Explicit source-neutralization wording used for manual debugging:
        # "找出冻融次数为 0 且抗体浓度大于 50 的样本，其样本来源实验为中和实验，返回这些来源中和实验对应的 LIMS 编号。"
        "query": "找出冻融次数为 0 且抗体浓度大于 50 的样本所对应的实验 LIMS 编号。",
        "needs_feedback": False,
    },
    "TC11": {
        "query": "查找昌平数据列描述表中，所有包含“业务术语”或“描述”定义的字段名。",
        "needs_feedback": False,
    },
    "TC14": {
        "query": "找出至少针对 3 种不同假病毒株开展过中和实验的抗体名称。",
        "needs_feedback": False,
    },
    "TC19": {
        "query": "帮我查一下那些还没有分配任何中和实验结果的实验 ID。",
        "needs_feedback": False,
    },
    "TC20": {
        "query": "告诉我抗体 SA58 的生产厂家和采购价格是多少？",
        "needs_feedback": False,
    },
    "TC25": {
        "query": "查查小张在两年前做的还没开始但已经做完的实验。",
        "needs_feedback": False,
    },
}

TC_QUERY_SEQUENCES = {
    "TC01": {
        "query": "哪些抗体的原始型别是 IgG1 且重链 V 基因是 IGHV3-53？",
        "needs_feedback": False,
    },
    "TC02": {
        "query": "帮我查一下原始型别是 IgG1，但重链V基因不是 IGHV3-53 的抗体 ID。",
        "needs_feedback": False,
    },
    "TC03": {
        "query": '查找所有针对别名包含 "Omicron" 的假病毒株所做的实验总数。',
        "needs_feedback": False,
    },
    "TC04": {
        "query": '统计目前处于"在库（STORED）"状态的抗体样本总数是多少？',
        "needs_feedback": False,
    },
    "TC05": {
        "query": "针对 JN.1 毒株，哪一个抗体样本表现出的中和活性最强？给出名称和最低IC50。",
        "needs_feedback": False,
    },
    "TC06": {
        "query": "针对 XBB.1 毒株，哪一个抗体表现出的中和活性最差？给出最高IC50。",
        "needs_feedback": False,
    },
    "TC07": {
        "query": "2026年第一季度（1月到3月底）由用户 Seeder 提交的实验都在什么状态？",
        "needs_feedback": False,
    },
    "TC08": {
        "query": "提取所有拟合成功的实验中，其详细信息（fit_info）里记录的 R2 拟合优度。",
        "needs_feedback": False,
    },
    "TC10": {
        "query": "哪些细胞株（cells）在库里目前没有任何可以使用的在库物理标本？",
        "needs_feedback": False,
    },
    "TC12": {
        "query": "每种假病毒系统在所有成功的中和实验中，平均的 IC50 值是多少？",
        "needs_feedback": False,
    },
    "TC13": {
        "query": "找出那些稀释梯度点（dilution_factors）少于 5 个点的中和实验。",
        "needs_feedback": False,
    },
    "TC14": {
        "query": "找出至少针对 3 种不同假病毒株开展过中和实验的抗体名称。",
        "needs_feedback": False,
    },
    "TC15": {
        "query": "帮我看看有哪些活干完了（COMPLETED）的 ic50 实验，是用 3参数 模型算的？",
        "needs_feedback": False,
    },
    "TC16": {
        "query": "查找 BA286 （少点）和 KP. 2（带空格）的病毒 ID。",
        "needs_feedback": False,
    },
    "TC17": {
        "query": '有哪些实验已经开始，但是至今还没有录入"结束日期（end_date）"？',
        "needs_feedback": False,
    },
    "TC18": {
        "query": "帮我看看系统里有没有冻融次数小于0次，或者大于100次的异常抗体标本？",
        "needs_feedback": False,
    },
    "TC20": {
        "query": "告诉我抗体 SA58 的生产厂家和采购价格是多少？",
        "feedback_responses": ["生产厂家和采购价格字段不可用，确认按无法查询处理。"],
        "needs_feedback": True,
    },
    "TC21": {
        "query": '帮我查询抗体 "抗体999" 或 "SA99" 的所有重链 DNA 序列。',
        "feedback_responses": ["抗体999 和 SA99 不存在，确认按空结果处理。"],
        "needs_feedback": True,
    },
    "TC22": {
        "query": "帮我查询一下 SARS-CoV-1 (非典株) 在 HeLa 细胞上的中和原始读数。",
        "feedback_responses": ["SARS-CoV-1 和 HeLa 的对应原始读数不存在，确认按空结果处理。"],
        "needs_feedback": True,
    },
    "TC23": {
        "query": '提取实验结果中，fit_info 字段里记录的 "p_value" (显著性P值)。',
        "feedback_responses": ['fit_info 中不存在 "p_value" 键，确认按空结果处理。'],
        "needs_feedback": True,
    },
    "TC24": {
        "query": '找出所有实验状态同时是 "COMPLETED" 并且又是 "FAILED" 的实验。',
        "feedback_responses": ["该条件互相矛盾，确认按空结果处理。"],
        "needs_feedback": True,
    },
    "TC25": {
        "query": "查查小张在两年前做的还没开始但已经做完的实验。",
        "feedback_responses": ["小张不存在且条件互相矛盾，确认按空结果处理。"],
        "needs_feedback": True,
    },
}

TCW_QUERY_SEQUENCES = {
    "TC-W01": {
        "query": "创建 BD55-1111 和 XBB.1 的中和实验，使用 huh-7 细胞。操作人是 Seeder。",
        "feedback_responses": [
            "BD55-1111 用样本 11396，XBB.1 用样本 900412，huh-7 用样本 800001。请继续。",
            "确认，请创建该中和实验。",
            "改用空闲的 Mary 作为操作研究员。",
        ],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W02": {
        "query": "新建一个实验：用 BA286 毒株（注：错别字）和 SA 58（注：带空格）抗体在 293T 细胞上做中和。",
        "feedback_responses": [
            "BA.2.86 用样本 900410，SA58 用样本 903002，HEK293T-ACE2 用样本 900310。请继续。",
            "确认，请创建该中和实验。",
        ],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W03": {
        "query": "今天帮我提交一个中和实验，把抗体 SA55 和假病毒 KP.2 关联上，细胞用默认的。",
        "feedback_responses": ["SA55 样本余量不足，确认不要创建实验。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W04": {
        "query": "创建中和实验，抗体 BD55-1111 稀释梯度为 1, 2, 4, 8, 16 232。细胞使用 huh-7，毒株 JN.1。",
        "feedback_responses": [
            "BD55-1111 用样本 11396，JN.1 用样本 904002，huh-7 用样本 800001。请继续。",
            "确认按可解析的稀释梯度继续；如果参数不合法，请不要创建实验。",
            "确认使用 1, 2, 4, 8, 16, 232 这个稀释梯度继续创建实验。",
        ],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W05": {
        "query": (
            '在昌平数据列描述表中新建一个字段记录，列名叫 "neutralization_index"，'
            '属于表 "neutralization_data"，描述是"中和率指标"。'
        ),
        "feedback_responses": ["确认仅在数据字典表存在且支持写入时新增该字段记录。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W06": {
        "query": '创建抗体 "SA99"（注：不存在）和假病毒 JN.1 的中和实验。',
        "feedback_responses": ["SA99 不存在，确认不要创建实验。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W07": {
        "query": "使用 非典病毒株 (SARS-CoV-1) 在 HeLa 细胞上建一个全新的中和实验流程。",
        "feedback_responses": ["SARS-CoV-1 和 HeLa 不存在，确认不要创建实验。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W08": {
        "query": '帮我用抗体 SA55 建个中和实验。注：SA55 对应的所有样本在系统里的状态都是 "ARCHIVED"（已归档/不可用）。',
        "feedback_responses": ["SA55 样本不可用或余量不足，确认不要创建实验。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W09": {
        "query": "关联抗体 BD-368 插入新实验，要求必须指定使用那批冻融次数（freeze_count）大于 5 次的旧标本。",
        "feedback_responses": ["不存在 freeze_count 大于 5 的 BD-368 样本，确认不要创建实验。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W10": {
        "query": '新建一个中和实验流程，要求在插入时直接把它的实验状态（status）初始化为 "COMPLETED"（已完成）。',
        "feedback_responses": ["新建实验不能直接初始化为 COMPLETED，确认不要创建实验。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W11": {
        "query": "提交一个新实验，开始日期设为今天，结束日期（end_date）设为上个月。",
        "feedback_responses": ["结束日期不能早于开始日期，确认不要创建实验。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W12": {
        "query": '创建一个中和实验，操作人和审批提交人都写 "外来访客"。',
        "feedback_responses": ['"外来访客" 不是有效用户，确认不要创建实验。'],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W13": {
        "query": '把实验 LIMS ID 为 10 那个任务的状态紧急修改为 "ABORTED"（中止/取消），并录入今天的结束日期。',
        "feedback_responses": ["LIMS ID 10 不存在，确认不要修改任何实验。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W14": {
        "query": "把实验 ID 为 99999（注：不存在）的任务状态修改为 COMPLETED。",
        "feedback_responses": ["实验 99999 不存在，确认不要修改状态。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
    "TC-W15": {
        "query": "帮我把抗体表（antibodies）里 ID 是 1 的那条抗体元数据资产彻底删除（DELETE）。",
        "feedback_responses": ["抗体 ID 1 不存在，确认不要删除任何记录。"],
        "needs_feedback": True,
        "rollback_database": True,
    },
}

# TC09, TC11, and TC19 are intentionally excluded per the local TC-note import
# request.
TC_EXCLUDED_QUERY_KEYS = {"TC09", "TC11", "TC19"}

# These queries are runnable, but their expected SQL is not precise enough to
# serve as an automated semantic oracle yet.
TC_AMBIGUOUS_QUERY_KEYS: set[str] = set()
TC_PENDING_QUERY_KEYS: set[str] = set()

# These checks remain visible in every TC report, but intentionally do not
# fail the e2e run until their business interpretation is confirmed by hand.
TC_NON_BLOCKING_REVIEW_CASES = {
    "TC10": {
        "reason": (
            "The ontology maps usable in-stock physical samples to wet_samples.status = 'STORED', "
            "but every cell sample in this fixture is STORED. The empty result cannot distinguish that "
            "predicate from an omitted availability predicate."
        ),
        "status": "passed_with_non_discriminating_fixture",
        "skip_expected_assertion": True,
    },
    "TC22": {
        "reason": "The fixture currently proves the SARS-CoV-1/HeLa relation is absent, not a complete raw-readout query.",
    },
}


SLOW_QUERY_KEYS = ["create_experiment"]
FAST_QUERY_KEYS = [
    "find_antibody_neutralization",
    "find_recent_experiment",
    "count_cells",
    "count_viruses",
    "count_antibodies",
    "ask_recent_experiment_id",
]
# Subset of queries that are very fast (single nl2sql call, no multi-step exploration).
# Used by --quick for CI smoke testing (~5 min total).
QUICK_QUERY_KEYS = ["count_cells", "count_viruses", "count_antibodies"]
QUERY_GROUPS = {
    "default": QUERY_SEQUENCES,
    "bad_cases": BAD_CASE_QUERY_SEQUENCES,
    "TC": TC_QUERY_SEQUENCES,
    "TC-W": TCW_QUERY_SEQUENCES,
}

# Two-process session replay: Q1-Q3 run in process 0 (normal continuous
# conversation), Q4-Q7 run in process 1 (simulates process restart that
# resumes the same session from disk). This mirrors the real-world scenario
# where a long-running session survives an agent restart.
#
#   process 0 (no restart):  create_experiment → find_antibody_neutralization → find_recent_experiment
#   process 1 (restart):     count_cells → count_viruses → count_antibodies → ask_recent_experiment_id
#
# Only the first query of process 1 is a true "restart first call" — its
# cache hit rate measures whether bp1 (System) stays byte-stable
# across process restarts (the D6 metric). Within-process queries rely on
# bp3 (tail_anchor) for high hit rates and are NOT restarts.
PROCESS_0_KEYS = [
    "create_experiment",
    "find_antibody_neutralization",
    "find_recent_experiment",
]
PROCESS_1_KEYS = [
    "count_cells",
    "count_viruses",
    "count_antibodies",
    "ask_recent_experiment_id",
]

# =============================================================================
# Expected functional results (per-query correctness assertions)
# =============================================================================
# These constants define the expected agent output for each query. They are
# used by the per-query result assertions added to test_v3_session_replay() to
# catch functional regressions (e.g. NL2SQL generating wrong SQL that prevents
# the experiment from being created).
#
# Values are verified directly against tests/e2e/bio_lab/data/bio_lab.sqlite
# and cross-checked against a known-good deepseek run
# (cache_test_user_v3_20260630_163422_9a55, which created experiment 902036).
#
# Q1 (create_experiment): the agent should INSERT a new neutralization
# experiment into the DB. The new row in neutralization_experiments (joined
# with experiments) must satisfy:
#   - inhibitor_sample_id = 11396  (antibody sample whose antibody_id=11397,
#     whose proteins.name='BD55-1111')
#   - pseudovirus_sample_id = 904036  (sample whose pseudovirus_id=1000401,
#     whose pseudoviruses.name='XBB.1.5')
#   - cell_sample_id = 800001  (sample whose cell_id=800002, cells.name='huh-7')
#   - experiments.status = 'NEW'
#   - experiments.start_date = today (UTC date of the test run)
# The new experiment id is auto-generated by the agent's INSERT script; it
# cannot be predicted ahead of time, so we capture it from the DB after Q1
# and use it for Q3 / Q7 consistency assertions.
EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID = 11396
EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID = 904036
EXPECTED_HUH7_CELL_SAMPLE_ID = 800001
EXPECTED_NEW_EXPERIMENT_STATUS = "NEW"

# Q2 (find_antibody_neutralization): BD-368 effectively neutralizes (IC50 < 0.1
# AND fit_success=1) these 5 pseudoviruses. Verified against the DB:
#   SELECT DISTINCT pv.name FROM ... WHERE proteins.name='BD-368'
#   AND nifd.fit_success=1 AND nifd.ic50 < 0.1;
EXPECTED_BD368_NEUTRALIZED_PSEUDOVIRUSES = {
    "EG.5",
    "JN.1",
    "HK.3",
    "BA.2.86",
    "KP.2",
}

# Q4-Q6 (count queries): the queries now explicitly say "有多少个不同类型的X"
# ("how many DIFFERENT TYPES of X"), so the expected answer is the total count
# of distinct entities in the DB. Verified directly:
#   SELECT COUNT(*) FROM cells;            -> 2   (800002 huh-7, 900300 HEK293T-ACE2)
#   SELECT COUNT(*) FROM pseudoviruses;    -> 7
#   SELECT COUNT(*) FROM antibodies;       -> 8
EXPECTED_CELL_COUNT = 2
EXPECTED_CELL_IDS = {"800002", "900300"}
EXPECTED_PSEUDOVIRUS_COUNT = 7
EXPECTED_PSEUDOVIRUS_IDS = {
    "900400",
    "900401",
    "901401",
    "901402",
    "901403",
    "901404",
    "1000401",
}
EXPECTED_ANTIBODY_COUNT = 8
EXPECTED_ANTIBODY_IDS = {
    "11397",
    "900500",
    "900501",
    "900502",
    "901500",
    "901501",
    "901502",
    "901503",
}

# =============================================================================
# Expected TC answer facts
# =============================================================================
# These values were produced once by running the expected SQL above against the
# current bio_lab.sqlite fixture. Runtime TC correctness checks assert the final
# answer contains these business facts; the SQL text remains above for debugging
# and for fixture-drift guard tests.
TC07_EXPECTED_EXPERIMENT_IDS = {
    "900100",
    "900110",
    "900120",
    "900200",
    "900210",
    "901000",
    "901001",
    "901002",
    "901003",
    "901004",
    "901005",
    "901006",
    "901007",
    "901010",
    "901011",
    "901014",
    "901015",
    "901016",
    "901017",
    "901019",
    "901022",
    "901026",
    "901027",
    "901034",
    "901036",
    "902000",
    "902001",
    "902002",
    "902003",
    "902004",
    "902005",
    "902006",
    "902007",
    "902010",
    "902011",
    "902014",
    "902015",
    "902016",
    "902017",
    "902019",
    "902022",
    "902026",
    "902027",
    "902034",
}
TC15_EXPECTED_EXPERIMENT_IDS = {
    "900200",
    "900210",
    "902000",
    "902001",
    "902002",
    "902003",
    "902004",
    "902005",
    "902006",
    "902007",
    "902008",
    "902009",
    "902010",
    "902011",
    "902012",
    "902013",
    "902014",
    "902015",
    "902016",
    "902017",
    "902018",
    "902019",
    "902020",
    "902021",
    "902022",
    "902023",
    "902024",
    "902025",
    "902026",
    "902027",
    "902028",
    "902029",
    "902030",
    "902031",
    "902032",
    "902033",
    "902034",
    "902035",
}
TC17_EXPECTED_EXPERIMENT_IDS = {
    "900100",
    "900110",
    "900120",
    "900200",
    "900210",
    "901000",
    "901001",
    "901002",
    "901003",
    "901004",
    "901005",
    "901006",
    "901007",
    "901008",
    "901009",
    "901010",
    "901011",
    "901012",
    "901013",
    "901014",
    "901015",
    "901016",
    "901017",
    "901018",
    "901019",
    "901020",
    "901021",
    "901022",
    "901023",
    "901024",
    "901025",
    "901026",
    "901027",
    "901028",
    "901029",
    "901030",
    "901031",
    "901032",
    "901033",
    "901034",
    "901035",
    "901036",
    "902000",
    "902001",
    "902002",
    "902003",
    "902004",
    "902005",
    "902006",
    "902007",
    "902008",
    "902009",
    "902010",
    "902011",
    "902012",
    "902013",
    "902014",
    "902015",
    "902016",
    "902017",
    "902018",
    "902019",
    "902020",
    "902021",
    "902022",
    "902023",
    "902024",
    "902025",
    "902026",
    "902027",
    "902028",
    "902029",
    "902030",
    "902031",
    "902032",
    "902033",
    "902034",
    "902035",
}

TC_ABSENCE_ANSWER_TERMS = ["不存在", "未找到", "没有", "为空", "无结果", "无法", "不包含", "未发现"]
TC_EXPECTED_ANSWER_ASSERTIONS = {
    "TC01": {
        "expected_summary": "3 IgG1 / IGHV3-53 antibodies: BD-368, SA58, S2E12.",
        "required_terms": ["3"],
        "soft_required_terms": ["IgG1", "IGHV3-53"],
        "required_any_term_groups": [["900500", "BD-368"], ["901500", "SA58"], ["901503", "S2E12"]],
    },
    "TC02": {
        "expected_summary": "4 IgG1 antibodies whose heavy V gene is not IGHV3-53.",
        "required_terms": ["4", "900501", "900502", "901501", "901502"],
    },
    "TC03": {
        "expected_summary": "21 neutralization experiments for pseudoviruses whose aliases contain Omicron.",
        "required_terms": ["21"],
    },
    "TC04": {
        "expected_summary": "46 antibody samples are STORED.",
        "required_terms": ["46"],
    },
    "TC05": {
        "expected_summary": "JN.1 strongest neutralizing antibody sample is BD-368, IC50 0.018614.",
        "required_terms": ["BD-368", "0.018614"],
    },
    "TC06": {
        "expected_summary": "XBB.1 worst neutralizing antibody is LY-CoV1404, IC50 0.177275.",
        "required_terms": ["LY-CoV1404", "0.177275"],
    },
    "TC07": {
        "expected_summary": "44 Seeder experiments in 2026 Q1; 901036 is NEW and the rest are COMPLETED.",
        "required_terms": ["44", "43", "1", "COMPLETED", "NEW"],
    },
    "TC08": {
        "expected_summary": "30 successful fits; fit data 900700 / experiment 900200 has R2 0.98 and the rest lack r2.",
        "required_terms": ["0.98"],
    },
    "TC10": {
        "blocking": False,
        "expected_summary": "No cells lack usable STORED physical samples; fixture is non-discriminating.",
        "soft_required_terms": ["细胞"],
        "absence_answer_terms": TC_ABSENCE_ANSWER_TERMS,
    },
    "TC12": {
        "expected_summary": "Successful neutralization average IC50 by pseudovirus system: VSV 0.1648.",
        "required_terms": ["VSV", "0.1648"],
    },
    "TC13": {
        "expected_summary": "4 neutralization experiments have fewer than 5 dilution points.",
        "required_terms": ["4", "900100", "900110", "900120", "901036"],
    },
    "TC14": {
        "expected_summary": "Antibodies tested against at least 3 pseudoviruses: BD-368, COV2-2196, LY-CoV1404, S2E12, SA58.",
        "required_terms": ["BD-368", "6", "COV2-2196", "3", "LY-CoV1404", "4", "S2E12", "SA58", "5"],
    },
    "TC15": {
        "expected_summary": "38 COMPLETED ic50_fit experiments use the 3-param logistic model.",
        "required_terms": ["38"],
        "soft_required_terms": ["COMPLETED", "3-param logistic"],
    },
    "TC16": {
        "expected_summary": "BA286/KP. 2 resolves to BA.2.86 900400 and KP.2 901403.",
        "required_terms": ["900400", "BA.2.86", "901403", "KP.2"],
    },
    "TC17": {
        "expected_summary": "78 experiments have start_date populated and end_date empty.",
        "required_terms": ["78"],
        "soft_required_terms": ["COMPLETED", "NEW"],
    },
    "TC18": {
        "expected_summary": "No abnormal antibody wet samples have freeze_count < 0 or > 100.",
        "soft_required_terms": ["冻融", "异常"],
        "absence_answer_terms": TC_ABSENCE_ANSWER_TERMS,
    },
    "TC20": {
        "allow_human_feedback_pass": True,
        "expected_summary": "SA58 manufacturer and purchase price are unavailable in the schema.",
        "soft_required_terms": ["SA58", "生产厂家", "采购价格"],
        "absence_answer_terms": ["没有", "未", "无法", "不支持"],
    },
    "TC21": {
        "allow_human_feedback_pass": True,
        "expected_summary": "Antibodies 抗体999 and SA99 do not exist, so no heavy-chain DNA sequence is available.",
        "soft_required_terms": ["抗体999", "SA99"],
        "absence_answer_terms": TC_ABSENCE_ANSWER_TERMS,
    },
    "TC22": {
        "allow_human_feedback_pass": True,
        "expected_summary": "No SARS-CoV-1 / HeLa neutralization raw-readout relation exists in the fixture.",
        "soft_required_terms": ["SARS-CoV-1", "HeLa"],
        "absence_answer_terms": TC_ABSENCE_ANSWER_TERMS,
    },
    "TC23": {
        "allow_human_feedback_pass": True,
        "expected_summary": "fit_info.p_value is absent for successful fit data.",
        "soft_required_terms": ["p_value"],
        "sql_evidence": {
            "required_sql_terms": ["p_value"],
            "null_column": 1,
            "allow_empty_result": True,
        },
    },
    "TC24": {
        "allow_human_feedback_pass": True,
        "expected_summary": "No experiment can be both COMPLETED and FAILED.",
        "soft_required_terms": ["COMPLETED", "FAILED"],
        "absence_answer_terms": TC_ABSENCE_ANSWER_TERMS,
    },
    "TC25": {
        "allow_human_feedback_pass": True,
        "expected_summary": "User 小张 does not exist and the requested status condition is contradictory.",
        "soft_required_terms": ["小张"],
        "absence_answer_terms": TC_ABSENCE_ANSWER_TERMS,
    },
}

TCW_EXPECTED_ANSWER_ASSERTIONS = {
    "TC-W01": {
        "blocking": False,
        "expected_summary": "Creates a NEW neutralization experiment for BD55-1111, XBB.1, and huh-7.",
        "required_terms": ["BD55-1111", "XBB.1", "huh-7"],
    },
    "TC-W02": {
        "blocking": False,
        "expected_summary": "Resolves BA286/SA 58 to BA.2.86 and SA58, using a 293T cell sample.",
        "required_any_term_groups": [["BA.2.86", "BA286"], ["SA58", "SA 58"], ["293T", "HEK293T-ACE2"]],
    },
    "TC-W03": {
        "allow_human_feedback_pass": True,
        "expected_summary": "SA55 / KP.2 request should stop because the SA55 sample volume is insufficient.",
        "required_terms": ["SA55", "KP.2"],
    },
    "TC-W04": {
        "blocking": False,
        "expected_summary": "BD55-1111 / JN.1 creation with non-structured dilution factors needs manual review.",
        "required_any_term_groups": [
            ["BD55-1111"],
            ["JN.1"],
            ["稀释", "dilution", "梯度"],
            ["huh-7"],
        ],
    },
    "TC-W05": {
        "blocking": False,
        "expected_summary": "Data dictionary table is absent; the answer should reference neutralization_data and report the no-write outcome.",
        "required_terms": ["neutralization_data"],
    },
    "TC-W06": {
        "allow_human_feedback_pass": True,
        "expected_summary": "SA99 does not exist, so the neutralization experiment must not be created.",
        "required_terms": ["SA99"],
    },
    "TC-W07": {
        "allow_human_feedback_pass": True,
        "expected_summary": "SARS-CoV-1 and HeLa are absent, so the creation request must be blocked.",
        "required_terms": ["SARS-CoV-1", "HeLa"],
    },
    "TC-W08": {
        "allow_human_feedback_pass": True,
        "expected_summary": "SA55 is not usable for creation because its only sample has insufficient volume.",
        "required_terms": ["SA55"],
    },
    "TC-W09": {
        "allow_human_feedback_pass": True,
        "expected_summary": "No BD-368 sample has freeze_count > 5, so the creation request must be blocked.",
        "required_terms": ["BD-368", "freeze_count"],
    },
    "TC-W10": {
        "allow_human_feedback_pass": True,
        "expected_summary": "A newly created neutralization experiment cannot be initialized as COMPLETED.",
        "required_terms": ["COMPLETED"],
    },
    "TC-W11": {
        "allow_human_feedback_pass": True,
        "expected_summary": "End date earlier than start date is a time-paradox; creation must be blocked.",
        "required_terms": ["结束日期", "开始日期"],
    },
    "TC-W12": {
        "allow_human_feedback_pass": True,
        "expected_summary": "Operator/submitter '外来访客' is not a valid user; creation must be blocked.",
        "required_terms": ["外来访客"],
    },
    "TC-W13": {
        "allow_human_feedback_pass": True,
        "expected_summary": "Experiment id=10 does not exist; UPDATE to ABORTED must be blocked.",
        "required_terms": ["10"],
    },
    "TC-W14": {
        "allow_human_feedback_pass": True,
        "expected_summary": "Experiment id=99999 does not exist; status update to COMPLETED must be blocked.",
        "required_terms": ["99999"],
    },
    "TC-W15": {
        "allow_human_feedback_pass": True,
        "expected_summary": "Antibody id=1 does not exist; DELETE must be blocked.",
        "required_terms": ["1"],
    },
}

ALL_EXPECTED_ANSWER_ASSERTIONS = {
    **TC_EXPECTED_ANSWER_ASSERTIONS,
    **TCW_EXPECTED_ANSWER_ASSERTIONS,
}

TCW_DB_EFFECT_ASSERTIONS = {
    "TC-W01": {
        "kind": "neutralization_created",
        "antibody_name": "BD55-1111",
        "pseudovirus_name": "XBB.1",
        "cell_name": "huh-7",
        "status": EXPECTED_NEW_EXPERIMENT_STATUS,
    },
    "TC-W02": {
        "kind": "neutralization_created",
        "antibody_name": "SA58",
        "pseudovirus_name": "BA.2.86",
        "cell_name": "HEK293T-ACE2",
        "status": EXPECTED_NEW_EXPERIMENT_STATUS,
    },
    "TC-W03": {
        "kind": "experiment_not_created",
        "antibody_name": "SA55",
        "pseudovirus_name": "KP.2",
    },
    "TC-W04": {
        "kind": "neutralization_created",
        "antibody_name": "BD55-1111",
        "pseudovirus_name": "JN.1",
        "cell_name": "huh-7",
        "status": EXPECTED_NEW_EXPERIMENT_STATUS,
        "blocking": False,
    },
    "TC-W05": {
        "kind": "table_absent_no_write",
        "table_globs": ["%column%", "%dictionary%", "%changping%"],
    },
    "TC-W06": {
        "kind": "experiment_not_created",
        "antibody_name": "SA99",
        "pseudovirus_name": "JN.1",
    },
    "TC-W07": {
        "kind": "experiment_not_created",
        "pseudovirus_name": "SARS-CoV-1",
        "cell_name": "HeLa",
    },
    "TC-W08": {
        "kind": "experiment_not_created",
        "antibody_name": "SA55",
    },
    "TC-W09": {
        "kind": "experiment_not_created",
        "antibody_name": "BD-368",
    },
    "TC-W10": {
        "kind": "experiment_not_created",
        "status": "COMPLETED",
    },
    "TC-W11": {
        "kind": "experiment_not_created",
    },
    "TC-W12": {
        "kind": "experiment_not_created",
        "operator_name": "外来访客",
    },
    "TC-W13": {
        "kind": "experiment_not_modified",
        "experiment_id": 10,
        "forbidden_status": "ABORTED",
    },
    "TC-W14": {
        "kind": "experiment_not_modified",
        "experiment_id": 99999,
        "forbidden_status": "COMPLETED",
    },
    "TC-W15": {
        "kind": "antibody_not_deleted",
        "antibody_id": 1,
    },
}


def parse_query_numbers(value: str | None) -> list[int] | None:
    """Parse a comma-separated list of 1-based query numbers."""
    if value is None or not value.strip():
        return None
    numbers: list[int] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        if not item.isdigit():
            raise ValueError(f"--query_no only accepts positive integers, got {item!r}")
        num = int(item)
        if num < 1:
            raise ValueError(f"--query_no is 1-based, got {num}")
        numbers.append(num)
    return numbers or None


def select_query_keys(
    query_group: str = "default",
    skip_slow: bool = False,
    quick: bool = False,
    query_numbers: list[int] | None = None,
) -> list[str]:
    if query_group not in QUERY_GROUPS:
        raise ValueError(f"Unknown query group: {query_group!r}; supported: {sorted(QUERY_GROUPS)}")
    if quick and query_group != "default":
        raise ValueError("--quick can only be used with the default replay group; use --bad_cases without --quick")
    query_keys = list(QUICK_QUERY_KEYS) if quick else list(QUERY_GROUPS[query_group].keys())
    if query_group == "default" and skip_slow:
        query_keys = [key for key in query_keys if key not in SLOW_QUERY_KEYS]

    if not query_numbers:
        return query_keys

    selected: list[str] = []
    for num in query_numbers:
        if num > len(query_keys):
            raise ValueError(
                f"--query_no {num} is out of range for {query_group!r} group (available: 1..{len(query_keys)})"
            )
        selected.append(query_keys[num - 1])
    return selected


def query_sequence_for_group(query_group: str = "default") -> dict[str, dict[str, object]]:
    if query_group not in QUERY_GROUPS:
        raise ValueError(f"Unknown query group: {query_group!r}; supported: {sorted(QUERY_GROUPS)}")
    return QUERY_GROUPS[query_group]
