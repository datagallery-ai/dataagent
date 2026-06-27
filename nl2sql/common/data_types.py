"""Generator/Validator/Selector 共用的数据类型定义"""
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List


@dataclass
class SimpleDataItem:
    """Generator/Validator/Selector 共用的轻量数据项

    字段说明：
    - question: 自然语言问题
    - evidence: 问题提示信息
    - database_path: SQLite 数据库文件路径（Checker 执行验证必需）
    - database_schema_after_schema_linking: Schema Linker 输出的 DDL 格式 schema 文本
    - question_id: 问题 ID（ICL 生成器按 ID 查找 few-shot 示例）
    - sql_guidance_items: SQL guidance 条目列表
    """
    question: str
    evidence: str
    database_path: str
    database_schema_after_schema_linking: str

    # 可选字段
    question_id: Optional[int] = None
    sql_guidance_items: Optional[List] = None


@dataclass
class TaggedSQL:
    """带来源标签的 SQL 候选

    source 取值："dc" | "skeleton" | "icl"，用于追溯生成回路。
    """
    sql: str
    source: str

    def to_dict(self) -> Dict[str, str]:
        return {"sql": self.sql, "source": self.source}


@dataclass
class SingleQuestionResult:
    """sql_generator 单题输出（对应 q_XXXX.json）

    token_usage 结构:
    {
        "generation": {
            "dc": {"input_tokens": N, "output_tokens": N, "reasoning_tokens": N, "content_tokens": N},
            "skeleton": {"input_tokens": N, "output_tokens": N, "reasoning_tokens": N, "content_tokens": N},
            "icl": {"input_tokens": N, "output_tokens": N, "reasoning_tokens": N, "content_tokens": N}
        },
        "validation": {"input_tokens": N, "output_tokens": N, "reasoning_tokens": N, "content_tokens": N}
    }
    注：reasoning_tokens 为 API 返回的真实值（开启思考模式时>0），content_tokens = output_tokens - reasoning_tokens
    """
    question_id: int
    db_id: str
    sql_candidates: List[TaggedSQL]
    sql_candidates_after_revision: List[TaggedSQL]
    token_usage: Dict[str, Any]

    def to_dict(self) -> dict:
        """序列化为 q_XXXX.json 格式"""
        return {
            "question_id": self.question_id,
            "db_id": self.db_id,
            "sql_candidates": [c.to_dict() for c in self.sql_candidates],
            "sql_candidates_after_revision": [c.to_dict() for c in self.sql_candidates_after_revision],
            "token_usage": self.token_usage,
        }


@dataclass
class SelectionResult:
    """sql_selector 单题输出（对应 q_XXXX_selected.json）

    在 SingleQuestionResult 基础上扩展 SQL 选择信息。
    token_usage 结构继承 generation + validation 并新增 selection 字段。
    status 取值：single / shortcut / full_review / fallback / fallback_no_llm / sweeper_stage1_no_FR
    """
    question_id: int
    db_id: str
    sql_candidates: List[TaggedSQL]
    sql_candidates_after_revision: List[TaggedSQL]
    confidence: float
    top1_sql: str
    status: str  # "single" | "shortcut" | "full_review" | "fallback" | "fallback_no_llm" | "sweeper_stage1_no_FR"
    full_review_always_enabled: bool
    # FR_force_on=true 时填充 full_review_sql；FR_force_on=false 时填充 selector_output_sql
    full_review_sql: Optional[str] = None
    selector_output_sql: Optional[str] = None
    token_usage: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为 q_XXXX_selected.json 格式"""
        d = {
            "question_id": self.question_id,
            "db_id": self.db_id,
            "sql_candidates": [c.to_dict() for c in self.sql_candidates],
            "sql_candidates_after_revision": [c.to_dict() for c in self.sql_candidates_after_revision],
            "confidence": self.confidence,
            "top1_sql": self.top1_sql,
            "full_review_always_enabled": self.full_review_always_enabled,
            "token_usage": self.token_usage,
        }
        if self.full_review_always_enabled:
            # FR_force_on=true 模式
            if self.full_review_sql is not None:
                d["full_review_sql"] = self.full_review_sql
        else:
            # FR_force_on=false 模式
            if self.selector_output_sql is not None:
                d["selector_output_sql"] = self.selector_output_sql
            d["status"] = self.status
        return d
