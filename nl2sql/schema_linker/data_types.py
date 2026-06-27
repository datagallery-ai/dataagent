"""
NL2SQL Schema Linking 数据类型定义
"""
from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict


class DataItem(BaseModel):
    """Schema linking pipeline 数据项"""
    question_id: int = Field(..., description="The question id of the data item")
    question: str = Field(..., description="The question of the data item")
    evidence: str = Field(default="", description="The evidence of the data item")
    gold_sql: str = Field(..., description="The gold sql of the data item")
    difficulty: str = Field(default="", description="The difficulty of the data item")
    database_id: str = Field(..., description="The database id of the data item")
    database_path: str = Field(..., description="The database path of the data item")
    database_schema: Dict[str, Any] = Field(..., description="The database schema of the data item")

    # Step 3: 关键词提取与值检索
    extracted_evidence: Optional[List[Dict[str, Any]]] = Field(default=None, description="LLM extracted evidence")
    question_keywords: Optional[List[str]] = Field(default=None, description="LLM extracted keywords")
    retrieved_values: Optional[Dict[str, Dict[str, Any]]] = Field(default=None, description="Retrieved values by table.column")
    database_schema_after_value_retrieval: Optional[Dict[str, Any]] = Field(default=None, description="Schema enhanced with retrieved values")

    # Step 4-9: Schema Linking 各步骤结果  
    column_match_tables_and_columns: Optional[Dict[str, List[str]]] = Field(default=None, description="Column semantic matching results")
    column_match_recall_path_detail: Optional[List[Dict[str, Any]]] = Field(default=None, description="Column semantic results with scores")
    llm_match_tables_and_columns: Optional[Dict[str, List[str]]] = Field(default=None, description="LLM direct linking results")
    value_match_lsh_tables_and_columns: Optional[Dict[str, List[str]]] = Field(default=None, description="Column value lsh matching results")
    value_match_lsh_recall_path_detail: Optional[List[Dict[str, Any]]] = Field(default=None, description="Column value lsh results with scores")
    value_match_lsh_values: Optional[Dict[str, List[Dict[str, Any]]]] = Field(default=None, description="LSH matched values by column_id")
    value_match_desc_tables_and_columns: Optional[Dict[str, List[str]]] = Field(default=None, description="Column value desc matching results")
    value_match_desc_recall_path_detail: Optional[List[Dict[str, Any]]] = Field(default=None, description="Column value desc results with scores")
    value_match_desc_values: Optional[Dict[str, List[Dict[str, Any]]]] = Field(default=None, description="Enum description matched values by column_id")
    value_retrieval_tables_and_columns: Optional[Dict[str, List[str]]] = Field(default=None, description="Value retrieval matching results")
    sql_reversed_tables_and_columns: Optional[Dict[str, List[str]]] = Field(default=None, description="SQL reversed linking results")
    join_closure_tables_and_columns: Optional[Dict[str, List[str]]] = Field(default=None, description="Join linking results")
    
    final_linked_tables_and_columns: Optional[Dict[str, List[str]]] = Field(default=None, description="merge schema linking results")
    database_schema_after_schema_linking: Optional[Dict[str, Any]] = Field(default=None, description="Schema enhanced with schema linking")

    # 生成 json 相关字段
    candidate_columns: Optional[Any] = Field(default=None)
    candidate_columns_detail: Optional[Any] = Field(default=None)
    recall_path_detail: Optional[Any] = Field(default=None)
    join_relations: Optional[Any] = Field(default=None)

    # 耗时与成本统计
    keyword_and_retrieval_time: Optional[float] = Field(default=None)
    column_match_time: Optional[float] = Field(default=None)
    llm_match_time: Optional[float] = Field(default=None)
    value_match_time: Optional[float] = Field(default=None)
    value_retrieval_time: Optional[float] = Field(default=None)
    sql_reversed_time: Optional[float] = Field(default=None)
    join_closure_time: Optional[float] = Field(default=None)
    total_time: Optional[float] = Field(default=None)

    keyword_llm_cost: Optional[Dict[str, Any]] = Field(default=None)
    llm_match_llm_cost: Optional[Dict[str, Any]] = Field(default=None)
    sql_reversed_llm_cost: Optional[Dict[str, Any]] = Field(default=None)
    total_llm_cost: Optional[Dict[str, Any]] = Field(default=None)
