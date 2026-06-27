"""BIRD官方评测对齐的SQL执行与比对模块

与 BIRD 官方 compare_sql 实现对齐：
- 使用 sqlite3.connect 直接连接（非只读URI模式）
- 使用 BEGIN TRANSACTION + rollback 事务机制
- 使用 set(tuples) == set(tuples) 比对（保留列序，保留行内重复值）
"""
import sqlite3
import threading
import logging
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)


def execute_sql_for_comparison(
    db_path: str, 
    sql: str, 
    timeout: int = 300
) -> Tuple[Optional[List[tuple]], Optional[str]]:
    """
    执行单条SQL用于比对（BIRD官方对齐）
    
    与BIRD evaluate_bird.py保持一致：
    - sqlite3.connect 连接（非只读URI模式）
    - BEGIN TRANSACTION + rollback（防止副作用）
    - cursor.fetchall() 返回原始 list of tuples
    
    Args:
        db_path: SQLite数据库文件路径
        sql: SQL查询语句
        timeout: 超时时间（秒）
        
    Returns:
        (result_rows, error_message)
        - result_rows: cursor.fetchall() 原始返回的 list of tuples，执行失败时为 None
        - error_message: 错误信息，成功时为 None
    """
    result = {'rows': None, 'error': None}
    
    def _execute():
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            conn.execute("BEGIN TRANSACTION;")
            cursor.execute(sql)
            result['rows'] = cursor.fetchall()
            conn.rollback()
        except sqlite3.DatabaseError as e:
            result['error'] = str(e)
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
        except Exception as e:
            result['error'] = str(e)
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            if conn:
                conn.close()
    
    thread = threading.Thread(target=_execute)
    thread.daemon = True
    thread.start()
    thread.join(timeout)
    
    if thread.is_alive():
        return None, f"SQL execution timed out after {timeout} seconds"
    
    return result['rows'], result['error']


def compare_sql_results(
    pred_sql: str, 
    gold_sql: str, 
    db_path: str, 
    timeout: int = 300
) -> int:
    """
    比对两个SQL的执行结果（BIRD官方对齐）
    
    与 evaluate_bird.py 第95-116行的 compare_sql 完全一致：
    1. 在同一个连接、同一个事务内依次执行 pred_sql 和 gold_sql
    2. 使用 set(predicted_res) == set(ground_truth_res) 比对
    
    比对特性（与BIRD官方一致）：
    - 忽略行序（set无序）
    - 保留列序（tuple有序，(1, 'a') != ('a', 1)）
    - 保留行内重复值（tuple保留重复，(1, 1) != (1,)）
    - 去重相同行（set去重）
    
    Args:
        pred_sql: 预测SQL
        gold_sql: Golden SQL
        db_path: 数据库文件路径
        timeout: 执行超时（秒），覆盖两个SQL的总执行时间
        
    Returns:
        1: 执行结果匹配
        0: 执行结果不匹配或执行失败
    """
    result = {'correctness': 0}
    
    def _compare():
        # 与BIRD官方compare_sql完全一致的实现
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            conn.execute("BEGIN TRANSACTION;")
            cursor.execute(pred_sql)
            predicted_res = cursor.fetchall()
            cursor.execute(gold_sql)
            ground_truth_res = cursor.fetchall()
            if set(predicted_res) == set(ground_truth_res):
                result['correctness'] = 1
            conn.rollback()
        except sqlite3.DatabaseError:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
        except Exception:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            if conn:
                conn.close()
    
    thread = threading.Thread(target=_compare)
    thread.daemon = True
    thread.start()
    thread.join(timeout)
    
    if thread.is_alive():
        logger.warning(f"SQL comparison timed out after {timeout}s")
        return 0
    
    return result['correctness']


def eval_ex(
    pred_sql: str, 
    gold_sql: str, 
    db_path: str, 
    timeout: int = 300
) -> Optional[int]:
    """
    执行准确率验证（Execution Accuracy）— BIRD官方对齐
    
    在 compare_sql_results 基础上增加 gold_sql 执行失败检测：
    - gold_sql 执行失败时返回 None（无法评测）
    - pred_sql 执行失败时返回 0（不匹配）
    - 两者都执行成功时，按 BIRD 官方标准比对
    
    Args:
        pred_sql: 预测SQL
        gold_sql: Golden SQL
        db_path: 数据库路径
        timeout: 每条SQL执行超时（秒）
        
    Returns:
        1: 执行结果匹配
        0: 执行结果不匹配
        None: gold_sql执行失败（无法评测）
    """
    try:
        # 先单独检查gold_sql是否能执行成功
        gold_rows, gold_error = execute_sql_for_comparison(db_path, gold_sql, timeout)
        if gold_rows is None:
            logger.warning(f"Gold SQL execution failed: {gold_error}")
            return None
        
        # 再检查pred_sql是否能执行成功
        pred_rows, pred_error = execute_sql_for_comparison(db_path, pred_sql, timeout)
        if pred_rows is None:
            return 0
        
        # BIRD官方对齐比对：set(tuples) == set(tuples)
        return 1 if set(pred_rows) == set(gold_rows) else 0
    except Exception as e:
        logger.error(f"Evaluation error: {e}")
        return 0


def eval_candidates(
    sql_candidates: List[str], 
    gold_sql: str, 
    db_path: str
) -> Tuple[Optional[int], int]:
    """
    验证多个SQL候选，任一匹配即判定为正确（BIRD官方对齐）
    
    Args:
        sql_candidates: SQL候选列表
        gold_sql: Golden SQL
        db_path: 数据库路径
        
    Returns:
        (is_correct, best_match_idx): 
            1/0 = 正确/错误, 最佳匹配的候选索引（-1表示无匹配）
            None = gold_sql执行失败（无法评测）
    """
    # 先检查gold_sql是否能执行
    gold_rows, gold_error = execute_sql_for_comparison(db_path, gold_sql)
    if gold_rows is None:
        logger.warning(f"Gold SQL execution failed: {gold_error}")
        return None, -1
    
    # gold_sql可执行，逐个比对候选
    for idx, sql in enumerate(sql_candidates):
        pred_rows, pred_error = execute_sql_for_comparison(db_path, sql)
        if pred_rows is not None and set(pred_rows) == set(gold_rows):
            return 1, idx
    return 0, -1
