"""日志工具函数

提供 qp() helper，为日志消息生成统一的 [q_XXXX] 前缀，
用于在多线程并行处理时区分不同题目的日志输出。
"""


def qp(source=None) -> str:
    """生成日志 question ID 前缀

    Args:
        source: SimpleDataItem（取 .question_id）、int、或 None

    Returns:
        "[q_XXXX] " 格式的前缀字符串，或空字符串 ""

    Examples:
        >>> qp(720)
        '[q_0720] '
        >>> qp(None)
        ''
    """
    if source is None:
        return ""
    if isinstance(source, int):
        qid = source
    elif hasattr(source, 'question_id'):
        qid = source.question_id
    else:
        return ""
    return f"[q_{qid:04d}] "
