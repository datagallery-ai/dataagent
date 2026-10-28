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
"""Dialect checks migrated from de_agent Suite; no runtime or model dependencies."""

import re
from pathlib import Path

import sqlglot
from loguru import logger
from sqlglot import exp


def validate_sql_syntax(content: str) -> list[str]:
    """Parse the original SQL using the Spark dialect."""
    if not content.strip():
        return ["SQL 文件不能为空"]
    try:
        sqlglot.parse(content, read="hive", error_level=sqlglot.errors.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError as exc:
        return [f"SQL语法校验失败：{exc}"]
    return []


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

    valid_target, _, _ = _validate_target_table_with_database(file_path)
    if not valid_target:
        errors.append(
            f"DDL校验失败: 第{_line_no(content, 0)}行 - DDL 中目标表缺少库名，请使用 `库名.表名` 格式"
        )

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
                errors.append(
                    f"DDL校验失败: 第{_line_no(content, partition_match.start())}行 - 分区列必须以 pt_ 开头"
                )
            for pt_col in pt_cols:
                col_pattern = re.search(
                    rf"{pt_col}\s+STRING\s+COMMENT\s+[^\']*\'[^\']*分区[^\']*\'",
                    partition_cols,
                    re.IGNORECASE,
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
                errors.append(
                    f"DDL校验失败: 第{_line_no(content, full_pos)}行 - 分区列{pt_col}不应出现在字段列表中"
                )

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

    forbidden_types = [
        "DOUBLE",
        "FLOAT",
        "BOOLEAN",
        "DATE",
        "TIMESTAMP",
        "VARCHAR",
        "CHAR",
        "ARRAY",
        "MAP",
        "STRUCT",
    ]
    for ftype in forbidden_types:
        match = re.search(rf"\b{ftype}\b", content_upper)
        if match:
            col_pos = match.start()
            full_pos = block_offset + columns_block.find(ftype, col_pos)
            errors.append(
                f"DDL校验失败: 第{_line_no(full_content, full_pos)}行 - DDL禁止使用 {ftype} 类型"
            )

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
        r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:(\w+)\.)?(\w+)",
        ddl_content,
        re.IGNORECASE,
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

    table_comment_match = re.search(
        r"\)\s*COMMENT\s+['\"]([^'\"]+)['\"]", ddl_content, re.IGNORECASE | re.DOTALL
    )
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

            errors.append(
                f"- 第{line}行：检测到 {func}()，当前执行环境SparkSQL 3.1不支持，请改用 {replacement}()。"
            )

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
                r"COLLECT_LIST\s*\((?:[^()]|\([^()]*\))*\bORDER\s+BY\b",
                after_collect,
                re.IGNORECASE | re.DOTALL,
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
        errors.append(
            f"DML校验失败: 第{_line_no(content, pos)}行 - CTE定义后存在多余逗号，请删除逗号"
        )

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
                    _process_select_for_cte_validation(
                        cte_select, visible_ctes, errors, content, scope_start
                    )

            # 校验主查询（CTE 之后的最终 INSERT SELECT）
            main_select = tree.find(exp.Select)
            if main_select:
                # 主查询起始位置为 WITH 关键字之后
                with_match = re.search(r"\bWITH\b", content, re.IGNORECASE)
                scope_start = with_match.start() if with_match else 0
                _process_select_for_cte_validation(
                    main_select, visible_ctes, errors, content, scope_start
                )
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


def _build_alias_to_cte_mapping(
    select_node: exp.Select, visible_ctes: dict[str, set[str] | None]
) -> dict[str, str]:
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
                lateral_cols = lateral_alias_to_columns.get(col_table)
                if col_name not in lateral_cols:
                    line_no = _find_line_no(col_node, content, scope_start_char)
                    errors.append(
                        f"DML校验失败: 第{line_no}行 - LATERAL VIEW '{col_table}' does not have column '{col_name}' "
                        f"(available: {', '.join(sorted(lateral_cols))})"
                    )
            return
        cte_name = alias_to_cte.get(col_table)
        cte_columns = visible_ctes.get(cte_name)
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
        if all(col_name not in visible_ctes.get(c) for c in verifiable_ctes):
            line_no = _find_line_no(col_node, content, scope_start_char)
            cte_list = ", ".join(verifiable_ctes)
            errors.append(
                f"DML校验失败: 第{line_no}行 - Column '{col_name}' not found in any CTE source ({cte_list})"
            )


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
            _process_select_for_cte_validation(
                inner_select, inner_ctes, errors, content, scope_start_char
            )


def _line_no(content: str, pos: int) -> int:
    """根据字符位置计算行号（1-indexed）"""
    return content[:pos].count("\n") + 1
