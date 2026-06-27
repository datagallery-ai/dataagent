"""
Prompt 工厂 - 负责 prompt 的格式化与生成
"""
from typing import List, Dict, Any
from .prompt_templates import KEYWORDS_EXTRACTION_PROMPT, SCHEMA_SELECTION_PROMPT, SQL_BACKED_SELECTION_PROMPT


class PromptFactory:
    """Prompt 工厂"""
    
    @staticmethod
    def format_keywords_extraction_prompt(question: str, hint: str) -> str:
        return KEYWORDS_EXTRACTION_PROMPT.format(QUESTION=question, HINT=hint)
    
    @staticmethod
    def format_schema_selection_prompt(database_schema: str, question: str, hint: str) -> str:
        return SCHEMA_SELECTION_PROMPT.format(
            DATABASE_SCHEMA=database_schema,
            QUESTION=question,
            HINT=hint,
        )
    
    @staticmethod
    def format_sql_backed_selection_prompt(few_shot_examples: List[Dict[str, Any]], database_schema: str, question: str, hint: str) -> str:
        examples_str = "\n\n".join(
            f"[Example {i}]\nNL: {ex['question']}\nSQL: {ex['sql']}" for i, ex in enumerate(few_shot_examples, 1)
        )
        return SQL_BACKED_SELECTION_PROMPT.format(
            FEW_SHOT_EXAMPLES=examples_str,
            DATABASE_SCHEMA=database_schema,
            QUESTION=question,
            HINT=hint,
        )
