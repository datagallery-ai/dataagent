"""NL2SQL 统一配置（按 1~13 分区，对应预处理 / Schema Linking / 生成 / 选择 / 评测等阶段）"""
import os
from pathlib import Path

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"       # HuggingFace 镜像

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 全局路径与目录
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NL2SQL_DIR     = Path(__file__).resolve().parent
WORKSPACE_ROOT = NL2SQL_DIR / "workspace"
LOG_DIR        = NL2SQL_DIR / "log"

# 断点续传默认开启
DEFAULT_FORCE_RERUN = False

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. 数据源
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BIRD_DATA_DIR         = NL2SQL_DIR / "data" / "dev"           # BIRD 数据集根目录
DEV_JSON              = str(BIRD_DATA_DIR / "dev.json")
BIRD_DB_DIR           = str(BIRD_DATA_DIR / "dev_databases")
BIRD_TABLES_JSON      = os.environ.get("BIRD_TABLES_JSON", str(BIRD_DATA_DIR / "dev_tables.json"))
FEW_SHOT_PATH         = str(WORKSPACE_ROOT / "few_shot" / "few_shot_examples.json")
USE_DATABASE_DESCRIPTION = True

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. LLM
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 当前选择的LLM供应商: "deepseek_v4_flash" | "deepseek_v4_pro" | "or_glm51"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "deepseek_v4_flash")
# -- OpenRouter 平台公共配置 (BIRD 官方评测指定 API 供应商) --
_OR_API_BASE = "https://openrouter.ai/api/v1/chat/completions"
_OR_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "OpenRouter_key")
# LLM供应商配置映射: {provider_name: (api_base, model, api_key[, extra_body])}
# extra_body 为可选字典，会在请求时 merge 到 JSON body 中（如 cache_control）
LLM_PROVIDER_CONFIGS = {
    "deepseek_v4_flash": (
        "https://api.deepseek.com/v1/chat/completions",
        "deepseek-v4-flash",
        "API_KEY",
        {
            "thinking": {"type": "disabled"},
        },
    ),
    # ── OpenRouter 平台（共用 _OR_API_BASE + _OR_API_KEY，按 model slug 区分） ──
    "or_glm51": (
        _OR_API_BASE,
        "z-ai/glm-5.1",
        _OR_API_KEY,
        {   # Z.AI 官方 provider（全精度），禁用思考模式
            "provider": {
                "order": ["Z.AI"],
                "allow_fallbacks": False,  # 禁止回退到其他 provider
            },
            "reasoning": {"enabled": False},  # 关闭思考模式
        },
    )
}

def get_llm_config(provider: str = None):
    """
    获取指定LLM供应商的配置

    Args:
        provider: 供应商名称（默认使用 LLM_PROVIDER）

    Returns:
        (api_base, model, api_key, extra_body) 四元组
        extra_body 为可选字典，会在请求时 merge 到 JSON body 中
        环境变量 LLM_API_BASE / LLM_MODEL / LLM_API_KEY 可覆盖默认值
    """
    provider = provider or LLM_PROVIDER
    if provider not in LLM_PROVIDER_CONFIGS:
        raise ValueError(f"Unknown LLM provider: {provider}. Available: {list(LLM_PROVIDER_CONFIGS.keys())}")
    defaults = LLM_PROVIDER_CONFIGS[provider]
    api_base = os.environ.get("LLM_API_BASE", defaults[0])
    model = os.environ.get("LLM_MODEL", defaults[1])
    api_key = os.environ.get("LLM_API_KEY", defaults[2])
    extra_body = dict(defaults[3]) if len(defaults) > 3 else {}
    return api_base, model, api_key, extra_body

# -- LLM 连接参数（环境变量覆盖 > 供应商默认值） --
_provider_defaults = LLM_PROVIDER_CONFIGS.get(LLM_PROVIDER, LLM_PROVIDER_CONFIGS["deepseek_v4_flash"])
LLM_API_BASE   = os.environ.get("LLM_API_BASE",   _provider_defaults[0])
LLM_MODEL      = os.environ.get("LLM_MODEL",      _provider_defaults[1])
LLM_API_KEY    = os.environ.get("LLM_API_KEY",    _provider_defaults[2])
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "10")) # LLM 调用重试次数
LLM_RETRY_DELAY = int(os.environ.get("LLM_RETRY_DELAY", "2"))
LLM_VERIFY_SSL  = os.environ.get("LLM_VERIFY_SSL", "false").lower() not in {"0", "false", "no"}
LLM_BACKOFF_MULTIPLIER = float(os.environ.get("LLM_BACKOFF_MULTIPLIER", "2"))
LLM_TIMEOUT = (
    int(os.environ.get("LLM_CONNECT_TIMEOUT", "10")),
    int(os.environ.get("LLM_READ_TIMEOUT", "30")),
)
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8192"))
LLM_PARSE_FAIL_MAX_RETRIES = int(os.environ.get("LLM_PARSE_FAIL_MAX_RETRIES", "10")) # LLM 解析失败重试次数
# -- 模块化 temperature（各阶段可独立调整） --
LLM_TEMPERATURE_GENERATION = float(os.environ.get("LLM_TEMPERATURE_GENERATION", "0.3"))  # 生成阶段
LLM_TEMPERATURE_VALIDATION = float(os.environ.get("LLM_TEMPERATURE_VALIDATION", "0.3"))  # 校验阶段（checker）
LLM_TEMPERATURE_SELECTION  = float(os.environ.get("LLM_TEMPERATURE_SELECTION",  "0.3"))  # 选择阶段（selector）

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. LSH 索引参数（step1e 本地 datasketch 索引构建默认值）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LSH_NUM_PERM     = 128       # MinHash 签名维度
LSH_THRESHOLD    = 0.4       # Jaccard 相似度阈值
LSH_K            = 2         # Shingling 分片大小
LSH_SAMPLE_LIMIT = 2000      # 每列采样值上限

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Few-Shot 示例生成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# few-shot 训练缓存（train_embeddings.npy + train_cache.json，离线产出输入数据）
FEW_SHOT_CACHE_DIR    = NL2SQL_DIR / "data" / "few_shot_data"
FEW_SHOT_SELECT_MODEL = "sentence-transformers/all-mpnet-base-v2"
FEW_SHOT_NUM_EXAMPLES = 5        # 每题返回的 few-shot 示例数
FEW_SHOT_DEVICE       = "cpu"    # SentenceTransformer 计算设备


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Preprocess
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP1_PREPROCESS_WORKSPACE_DIR = Path(os.environ.get("STEP1_PREPROCESS_WORKSPACE_DIR", str(WORKSPACE_ROOT / "preprocess")))
STEP1_PREPROCESS_LOG_DIR = Path(os.environ.get("STEP1_PREPROCESS_LOG_DIR", str(LOG_DIR / "preprocess")))
STEP1_PREPROCESS_STATE_DIR_NAME = os.environ.get("STEP1_PREPROCESS_STATE_DIR_NAME", "state")
STEP1_PREPROCESS_SUMMARY_PATH = str(STEP1_PREPROCESS_WORKSPACE_DIR / "summary.json")
STEP1_PREPROCESS_RUN_LOG_PATH = str(STEP1_PREPROCESS_LOG_DIR / "step1_run_preprocess.log")
STEP1_COLUMN_VECTOR_STORE_DIR = str(STEP1_PREPROCESS_WORKSPACE_DIR / "column_vector_store")
STEP1_JOIN_RELATIONS_DIR = str(STEP1_PREPROCESS_WORKSPACE_DIR / "join_relations")
STEP1_LSH_INDEX_DIR = str(STEP1_PREPROCESS_WORKSPACE_DIR / "lsh_indexes")
STEP1_VALUE_DESC_ENUM_DIR = str(STEP1_PREPROCESS_WORKSPACE_DIR / "value_desc_enum")
STEP1_VALUE_DESC_VECTOR_DIR   = str(STEP1_PREPROCESS_WORKSPACE_DIR / "value_desc_vectors")
STEP1_CACHE_DIR               = str(STEP1_PREPROCESS_WORKSPACE_DIR / "rest_cache")
STEP1_VALUE_VECTOR_STORE_DIR  = str(STEP1_PREPROCESS_WORKSPACE_DIR / "vector_store")
# -- Step1a: Schema Cache --
STEP1A_SAMPLE_VALUE_COUNT = int(os.environ.get("STEP1A_SAMPLE_VALUE_COUNT", "3"))
# -- Step1b: Column Description Enhancement --
STEP1B_LLM_TEMPERATURE   = float(os.environ.get("STEP1B_LLM_TEMPERATURE", "0.7"))
STEP1B_FLUSH_INTERVAL    = int(os.environ.get("STEP1B_FLUSH_INTERVAL", "10"))
STEP1B_STAGING_DIR       = str(STEP1_PREPROCESS_WORKSPACE_DIR / "rest_cache" / "_columns_staging")
# -- Step1c: Column Vector Build --
STEP1C_TEXT_SEPARATOR = os.environ.get("STEP1C_TEXT_SEPARATOR", " | ")
# -- Step1d: Join Graph Build --
STEP1D_MAX_JOIN_DEPTH = int(os.environ.get("STEP1D_MAX_JOIN_DEPTH", "5"))
# -- Step1e: LSH Index Build --
STEP1E_LSH_NUM_PERM = int(os.environ.get("STEP1E_LSH_NUM_PERM", str(LSH_NUM_PERM)))
STEP1E_LSH_THRESHOLD = float(os.environ.get("STEP1E_LSH_THRESHOLD", str(LSH_THRESHOLD)))
STEP1E_LSH_K = int(os.environ.get("STEP1E_LSH_K", str(LSH_K)))
STEP1E_LSH_SAMPLE_LIMIT = int(os.environ.get("STEP1E_LSH_SAMPLE_LIMIT", str(LSH_SAMPLE_LIMIT)))
# -- Step1f1 / Step1f2: Value Description Enum Extraction + Vector Build --
STEP1F_TEXT_SEPARATOR = os.environ.get("STEP1F_TEXT_SEPARATOR", " | ")
STEP1F_ENUM_MAX_DISTINCT = int(os.environ.get("STEP1F_ENUM_MAX_DISTINCT", "50"))
STEP1F_ENUM_COVERAGE_THRESHOLD = float(os.environ.get("STEP1F_ENUM_COVERAGE_THRESHOLD", "0.6"))
STEP1F_ENUM_MAX_UNMATCHED = int(os.environ.get("STEP1F_ENUM_MAX_UNMATCHED", "5"))
STEP1F_ENUM_MIN_MAPPING_SIZE = int(os.environ.get("STEP1F_ENUM_MIN_MAPPING_SIZE", "2"))
STEP1F_ENUM_CASE_INSENSITIVE = os.environ.get("STEP1F_ENUM_CASE_INSENSITIVE", "false").lower() in {"1", "true", "yes"}
STEP1F_LLM_TEMPERATURE = float(os.environ.get("STEP1F_LLM_TEMPERATURE", "0"))
STEP1F_ENUM_IGNORE_COVERAGE_VALUES = ["N/A", "NA", "NULL", "NONE", "UNKNOWN"]
# step1f1 列级 staging 目录（仅枚举列产出 staging 文件；非枚举列不写文件，仅 add_completed_key）
STEP1F1_COLUMN_STAGING_DIR = str(STEP1_PREPROCESS_WORKSPACE_DIR / "value_desc_enum" / "_columns_staging")
# -- Step1g: Value Vector Build --
STEP1G_MAX_VALUES_PER_COLUMN = int(os.environ.get("STEP1G_MAX_VALUES_PER_COLUMN", "2000"))
STEP1G_MAX_VALUE_LENGTH = int(os.environ.get("STEP1G_MAX_VALUE_LENGTH", "100"))
STEP1G_ENCODE_BATCH_SIZE = int(os.environ.get("STEP1G_ENCODE_BATCH_SIZE", "512"))
STEP1G_LOWER_META_DATA = os.environ.get("STEP1G_LOWER_META_DATA", "true").lower() in {"1", "true", "yes"}
STEP1G_SKIP_UUID_ONLY_COLUMNS = os.environ.get("STEP1G_SKIP_UUID_ONLY_COLUMNS", "true").lower() in {"1", "true", "yes"}
STEP1G_SKIP_NUMBER_ONLY_COLUMNS = os.environ.get("STEP1G_SKIP_NUMBER_ONLY_COLUMNS", "true").lower() in {"1", "true", "yes"}
# -- Step1h: Few-Shot Examples Builder --
# 题目级 staging 目录
STEP1H_QUESTION_STAGING_DIR = str(WORKSPACE_ROOT / "few_shot" / "_questions_staging")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. Schema Linking
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCHEMA_LINKER_LOG_DIR = LOG_DIR / "schema_linker"
WORKSPACE_DIR         = WORKSPACE_ROOT / "schema_linker"
SCHEMA_LINKING_N_PARALLEL = 8
# 每处理多少题写一次 checkpoint（dataset.pkl）
SCHEMA_LINKING_SAVE_INTERVAL = int(os.environ.get("SCHEMA_LINKING_SAVE_INTERVAL", "20"))
# -- Embedding --
EMBEDDING_MODEL  = "BAAI/bge-large-zh-v1.5"
EMBEDDING_DEVICE = "cpu"
# -- Step2 各子步骤中间结果路径（Step2a ~ Step2i） --
STEP2A_DATASET_SAVE_PATH         = str(WORKSPACE_DIR / "dataset"        / "dataset.pkl")
STEP2B_KEYWORDS_SAVE_PATH        = str(WORKSPACE_DIR / "keywords"       / "dataset.pkl")
STEP2C_COLUMN_MATCH_SAVE_PATH    = str(WORKSPACE_DIR / "column_match"   / "dataset.pkl")
STEP2D_LLM_DIRECT_SAVE_PATH      = str(WORKSPACE_DIR / "llm_direct"     / "dataset.pkl")
STEP2E_COLUMN_VALUE_SAVE_PATH    = str(WORKSPACE_DIR / "column_value"   / "dataset.pkl")
STEP2F_VALUE_RETRIEVAL_SAVE_PATH = str(WORKSPACE_DIR / "value_retrieval"/ "dataset.pkl")
STEP2G_SQL_REVERSED_SAVE_PATH    = str(WORKSPACE_DIR / "sql_reversed"   / "dataset.pkl")
STEP2H_JOIN_CLOSURE_SAVE_PATH    = str(WORKSPACE_DIR / "join_closure"   / "dataset.pkl")
STEP2I_OUTPUT_DIR                = str(WORKSPACE_DIR / "output")
STEP2I_OUTPUT_DDL_DIR            = str(WORKSPACE_DIR / "ddl_output")
STEP2_STATE_PATH                 = str(SCHEMA_LINKER_LOG_DIR / "state.json")
# -- Step2c: Column Match Linker --
COLUMN_SEMANTIC_SCORE_THRESHOLD = float(os.environ.get("COLUMN_SEMANTIC_SCORE_THRESHOLD", "1.37"))
COLUMN_SEMANTIC_MATCH_TOP_K     = int(os.environ.get("COLUMN_SEMANTIC_MATCH_TOP_K", "5"))
COLUMN_SEMANTIC_MATCH_VECTOR_BOOST     = int(os.environ.get("COLUMN_SEMANTIC_MATCH_VECTOR_BOOST", "1"))
COLUMN_SEMANTIC_MATCH_TEXT_BOOST     = int(os.environ.get("COLUMN_SEMANTIC_MATCH_TEXT_BOOST", "1"))
# -- Step2d: LLM Direct Linker --
LLM_DIRECT_LINKING_BUDGET = 5
# -- Step2e: Value Match Linker --
VALUE_LSH_SCORE_THRESHOLD  = float(os.environ.get("VALUE_LSH_SCORE_THRESHOLD",  "0.42"))
VALUE_DESC_SCORE_THRESHOLD = float(os.environ.get("VALUE_DESC_SCORE_THRESHOLD", "0.25"))
VALUE_MATCH_TOP_K          = int(os.environ.get("VALUE_MATCH_TOP_K", "3"))
# -- Step2f & Step2h: Value Retrieval --
VALUE_RETRIEVAL_N_RESULTS  = 5
LOWER_META_DATA            = True
VALUE_DISTANCE_THRESHOLD   = 0.05
# -- Step2g: SQL Reversed Linker --
REVERSED_LINKING_BUDGET = 5
# -- Step2h: Join Closure Linker --
JOIN_MAX_DEPTH = int(os.environ.get("JOIN_MAX_DEPTH", "5"))
# -- Step2i: Format Output --
OUTPUT_DDL_VALUE_EXAMPLE_MAX_COUNT = 3
OUTPUT_DDL_WITH_PATH_RECALL_FIRST = ["column_match", "llm_match", "value_match_lsh", "value_match_desc", "value_retrieval", "sql_reversed", "join_closure"]
OUTPUT_DDL_WITH_PATH_PRECISION_FIRST = ["llm_match", "value_match_lsh", "value_match_desc", "sql_reversed"]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8. Generation（SQL 生成 — generator + validator 结果输出）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GENERATION_OUTPUT_DIR = WORKSPACE_ROOT / "sql_generation"  # 结果输出目录（workspace/{run_id}/q_XXXX.json）
# -- CoT (Chain-of-Thought) 输出开关：启用后在 {run_id}/cot/ 下记录 LLM 调用轨迹 --
COT_OUTPUT_ENABLED = os.environ.get("NL2SQL_COT_OUTPUT", "1") not in ("0", "false", "False", "")
# -- 采样预算（每路生成器总预算 = INITIAL + ADDITIONAL）--
GENERATOR_DC_BUDGET       = int(os.environ.get("GENERATOR_DC_BUDGET", "5"))
GENERATOR_SKELETON_BUDGET = int(os.environ.get("GENERATOR_SKELETON_BUDGET", "5"))
GENERATOR_ICL_BUDGET      = int(os.environ.get("GENERATOR_ICL_BUDGET", "5"))
# -- 动态策略：Phase1 每路 INITIAL_BUDGET 条，判 single 后 Phase2 继续 --
GENERATOR_INITIAL_BUDGET  = int(os.environ.get("GENERATOR_INITIAL_BUDGET", "3"))
MAX_ROUTE_RETRY_ATTEMPTS = int(os.environ.get("MAX_ROUTE_RETRY_ATTEMPTS", "5"))
# -- 并行度 --
GENERATOR_MAX_WORKERS     = int(os.environ.get("GENERATOR_MAX_WORKERS", "3"))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9. Selection（SQL 选择 — selector）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SELECTOR_FILTER_TOP_K       = int(os.environ.get("SELECTOR_FILTER_TOP_K", "3"))       # Full Review 参与 LLM 投票的 Top-K 候选数
SELECTOR_EVALUATOR_BUDGET   = int(os.environ.get("SELECTOR_EVALUATOR_BUDGET", "1"))   # Top-K 裁决的 LLM 投票次数
SELECTOR_SHORTCUT_THRESHOLD = float(os.environ.get("SELECTOR_SHORTCUT_THRESHOLD", "0.5"))  # Shortcut 快捷路径阈值 (设为0：强制single/shortcut，不走full review)
SELECTOR_MAX_WORKERS        = int(os.environ.get("SELECTOR_MAX_WORKERS", "3"))        # Top-K 裁决并行度

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 10. Evaluation（SQL 评测 — BIRD 正确性验证）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EVAL_SQL_TIMEOUT = int(os.environ.get("EVAL_SQL_TIMEOUT", "300"))  # 评测SQL执行超时（秒）

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 11. Step3b Runner（单组合全流程）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP3B_RUN_ID             = os.environ.get("STEP3B_RUN_ID", "default_run")
STEP3B_SCHEMA_JSON        = os.environ.get("STEP3B_SCHEMA_JSON", "recall_first_schema.json")  # schema_linker/ddl_output/ 下的文件名
STEP3B_LLM_PROVIDER       = os.environ.get("STEP3B_LLM_PROVIDER", None)     # None 使用全局 LLM_PROVIDER
STEP3B_FR_FORCE_ON        = os.environ.get("STEP3B_FR_FORCE_ON", "false").lower() in {"1", "true", "yes"}
STEP3B_MAX_WORKERS        = int(os.environ.get("STEP3B_MAX_WORKERS", "5"))  # 题目级并行度（主控旋钮，峰值 LLM 并发 = 此值 * max(GEN, SEL)_MAX_WORKERS）
STEP3B_MAX_GLOBAL_RETRIES = int(os.environ.get("STEP3B_MAX_GLOBAL_RETRIES", "5"))  # 全局重试轮数
STEP3B_LOG_DIR            = LOG_DIR / "step3b"
STEP3B_SWEEP_LOG_DIR      = LOG_DIR / "step3b_sweeper_combination"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 12. Step4a SFT Selector
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SFT_SELECTOR_RUN_ID        = os.environ.get("SFT_SELECTOR_RUN_ID", "dev_run_example")
SFT_SELECTOR_MODEL_PATH    = os.environ.get("SFT_SELECTOR_MODEL_PATH", "/path/to/sft_selector_model")
SFT_SELECTOR_OUTPUT_NAME   = os.environ.get("SFT_SELECTOR_OUTPUT_NAME", "predict_dev_sft_selector.json")
