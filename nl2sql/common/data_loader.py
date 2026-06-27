"""Runner 公共数据加载函数

提供以下公共函数：
  - load_dev_questions(): 加载 dev.json 题目列表
  - load_ddl_schemas(): 加载 DDL schema JSON
  - build_data_item(): 组装 SimpleDataItem
  - create_llm(): 创建 LLMAdapter 实例
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict

from .. import config
from ..common.data_types import SimpleDataItem
from ..client import LLMAdapter

logger = logging.getLogger(__name__)


def load_dev_questions(dev_json_path: str = None) -> Dict[int, dict]:
    """从 dev.json 加载题目列表

    Args:
        dev_json_path: dev.json 路径（默认使用 config.DEV_JSON）

    Returns:
        {question_id: entry} 映射
    """
    path = dev_json_path or config.DEV_JSON
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {d["question_id"]: d for d in data}


def load_ddl_schemas(schema_json_name: str) -> Dict[int, str]:
    """从 workspace/schema_linker/ddl_output/{schema_json_name} 加载 DDL schema

    Args:
        schema_json_name: DDL schema 文件名（如 "ddl_4-13_idx31.json"）

    Returns:
        {question_id: db_desc} 映射
    """
    schema_path = Path(config.STEP2I_OUTPUT_DDL_DIR) / schema_json_name
    if not schema_path.exists():
        raise FileNotFoundError(f"DDL schema not found: {schema_path}")
    with open(schema_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {d["question_id"]: d["db_desc"] for d in data}


def build_data_item(
    question_id: int,
    dev_questions: Dict[int, dict],
    ddl_schemas: Dict[int, str],
) -> SimpleDataItem:
    """组装 SimpleDataItem

    Args:
        question_id: 题目 ID
        dev_questions: load_dev_questions() 返回的映射
        ddl_schemas: load_ddl_schemas() 返回的映射

    Returns:
        SimpleDataItem 实例

    Raises:
        KeyError: question_id 在 dev_questions 或 ddl_schemas 中不存在
    """
    q = dev_questions[question_id]
    db_id = q["db_id"]
    db_path = os.path.join(config.BIRD_DB_DIR, db_id, f"{db_id}.sqlite")
    schema_text = ddl_schemas[question_id]

    return SimpleDataItem(
        question=q["question"],
        evidence=q.get("evidence", ""),
        database_path=db_path,
        database_schema_after_schema_linking=schema_text,
        question_id=question_id,
    )


def create_llm(provider: str = None) -> LLMAdapter:
    """根据 provider 创建 LLMAdapter 实例

    Args:
        provider: LLM 供应商名称（默认使用 config.LLM_PROVIDER）

    Returns:
        LLMAdapter 实例
    """
    provider = provider or config.LLM_PROVIDER
    api_base, model, api_key, extra_body = config.get_llm_config(provider)
    logger.info(f"Creating LLMAdapter: provider={provider}, model={model}")
    return LLMAdapter(
        api_base=api_base,
        model=model,
        api_key=api_key,
        max_retries=config.LLM_MAX_RETRIES,
        retry_delay=config.LLM_RETRY_DELAY,
        backoff_multiplier=config.LLM_BACKOFF_MULTIPLIER,
        timeout=config.LLM_TIMEOUT,
        max_tokens=config.LLM_MAX_TOKENS,
        verify_ssl=config.LLM_VERIFY_SSL,
        extra_body=extra_body,
    )
