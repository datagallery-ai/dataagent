"""SQL rewrite regressions ported from !596 (e8d782b5)."""

import sys
from pathlib import Path

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"
_function = PythonLoader().load(PLUGIN, "utils/dataops_validate_tool.py:_replace_target_table")
vt = sys.modules[_function.__module__]


class TestAdhocTempTableGuard:
    """安全闸门：只有 ``adhoctemp.tmp_*`` 才允许被 DROP。"""

    def test_real_business_table_rejected(self):
        """真实业务表必须被拦住。"""
        assert vt._is_adhoctemp_temp_table("biads.ads_x") is False
        assert vt._is_adhoctemp_temp_table("ads_x") is False
        assert vt._is_adhoctemp_temp_table("") is False
        assert vt._is_adhoctemp_temp_table(None) is False

    def test_adhoctemp_scratch_table_allowed(self):
        """adhoctemp 里的临时表放行。"""
        assert vt._is_adhoctemp_temp_table("adhoctemp.tmp_user_20260101_ads_x") is True

    def test_create_like_does_not_drop_real_table(self):
        """``CREATE TABLE x LIKE y`` 不能生成对真实表的 DROP。"""
        drop_sql = vt._build_drop_sql(
            "CREATE TABLE biads.ads_x LIKE biads.src_x",
            user_account="nezha",
            table_suffix="20260101",
            partition_date="20260101",
        )
        assert drop_sql is None, f"must not DROP a real table, got {drop_sql!r}"

    def test_create_table_scratch_is_dropped(self):
        """正常的 CREATE TABLE 临时表仍然会被预清理。"""
        drop_sql = vt._build_drop_sql(
            "CREATE TABLE adhoctemp.tmp_nezha_20260101_ads_x (a INT)",
            user_account="nezha",
            table_suffix="20260101",
            partition_date="20260101",
        )
        assert drop_sql is not None
        assert "adhoctemp.tmp_" in drop_sql


class TestBacktickTableRefs:
    """反引号包裹的表名也要能改写。"""

    def test_insert_with_backticks_rewritten(self):
        rewritten = vt._replace_target_table(
            "INSERT OVERWRITE TABLE `biads`.`ads_x` SELECT 1",
            user_account="nezha",
            table_suffix="20260101",
            partition_date="20260101",
        )
        assert "adhoctemp.tmp_" in rewritten
        assert "`biads`.`ads_x`" not in rewritten

    def test_create_with_backticks_rewritten(self):
        rewritten = vt._replace_target_table(
            "CREATE TABLE `biads`.`ads_x` (a INT)",
            user_account="nezha",
            table_suffix="20260101",
            partition_date="20260101",
        )
        assert "adhoctemp.tmp_" in rewritten
        assert "`biads`.`ads_x`" not in rewritten

    def test_plain_table_still_rewritten(self):
        """普通写法不能被改坏。"""
        rewritten = vt._replace_target_table(
            "INSERT OVERWRITE TABLE biads.ads_x SELECT 1",
            user_account="nezha",
            table_suffix="20260101",
            partition_date="20260101",
        )
        assert "adhoctemp.tmp_" in rewritten
        assert "biads.ads_x" not in rewritten


class TestCommentAwareStatementDetection:
    """注释里的 ``create table`` 不能把 DML 误判成 DDL。

    早先 ``_is_create_sql`` 直接在原始 SQL 上跑正则 —— DML 文件里一句
    ``-- create table biads.ads_x 的定义见 DDL 文件`` 的说明注释就会被命中，
    于是这条 DML 被当成 DDL：wrapper 不再清理它的临时表，清理链路也抽不到
    表名 —— 临时表永久残留。
    """

    def test_line_comment_does_not_make_dml_look_like_ddl(self):
        sql = "-- create table biads.ads_x 的定义见 DDL 文件\nINSERT OVERWRITE TABLE biads.ads_x SELECT 1\n"
        assert vt._is_create_sql(sql) is False

    def test_block_comment_does_not_make_dml_look_like_ddl(self):
        sql = "/* create table biads.ads_x */\nINSERT OVERWRITE TABLE biads.ads_x SELECT 1\n"
        assert vt._is_create_sql(sql) is False

    def test_real_ddl_still_detected(self):
        sql = "CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_x (a INT)\n"
        assert vt._is_create_sql(sql) is True

    def test_string_literal_with_dashes_is_not_a_comment(self):
        """``'a--b'`` 是字符串字面量，不是注释。"""
        stripped = vt._strip_sql_comments("SELECT 'a--b' FROM t")
        assert "'a--b'" in stripped

    def test_comment_text_is_replaced_not_deleted(self):
        """注释用等长空格替换 —— 偏移量不变，正则匹配不受影响。"""
        sql = "-- create table x\nINSERT 1"
        stripped = vt._strip_sql_comments(sql)
        assert len(stripped) == len(sql)


class TestEscapedQuoteInStringLiteral:
    """字符串字面量里的转义引号不能把字符串状态翻过来。

    Spark / Hive 里字符串里的单引号可以用**反斜杠转义**：``'it\\'s'``。
    早先的 ``_strip_sql_comments`` 判断的是「引号**之后**的字符」是否为
    反斜杠（``sql[i + 1] == "\\\\"``），而反斜杠实际在引号**之前** ——
    所以 ``\\'`` 永远识别不出来。

    后果（整条链都实测过）：

    1. 扫描器在 ``\\'`` 处错误地关闭字符串 → 状态翻转；
    2. 字符串**之后**的真注释（``-- create table ...``）反而被当成代码保留；
    3. ``_is_create_sql`` 把 DML 误判成 DDL；
    4. ``needs_cleanup = not _is_create_sql(sql)`` 变成 False → **DML 的临时表
       永远不删** —— 正是本 PR 要修的 bug 原样复现。
    """

    ESCAPED = "WHERE note = 'it\\'s fine'"

    def test_escaped_quote_does_not_close_the_string(self):
        """``\\'`` 是字面量，字符串不能因此关闭。"""
        stripped = vt._strip_sql_comments(f"SELECT 1 {self.ESCAPED} -- create table x")
        assert "-- create table x" not in stripped
        assert self.ESCAPED in stripped

    def test_dml_with_escaped_quote_is_not_misread_as_ddl(self):
        """带转义引子的 DML 不能被误判成 DDL。"""
        sql = "INSERT OVERWRITE TABLE biads.ads_x PARTITION (pt_d = '20260101')\nSELECT id FROM biads.src WHERE note = 'it\\'s fine'\n-- create table biads.ads_x 定义见 DDL 文件\n"
        assert vt._is_create_sql(sql) is False

    def test_dml_with_escaped_quote_is_still_an_insert(self):
        sql = "INSERT OVERWRITE TABLE biads.ads_x PARTITION (pt_d = '20260101')\nSELECT id FROM biads.src WHERE note = 'it\\'s fine'\n"
        assert vt._is_insert_sql(sql) is True

    def test_doubled_quote_escape_does_not_close_the_string(self):
        """``''`` 是字符串里的一个单引号 —— 同样不能关闭字符串。"""
        sql = "SELECT 'it''s fine' -- create table x"
        stripped = vt._strip_sql_comments(sql)
        assert "-- create table x" not in stripped

    def test_comment_after_escaped_literal_is_stripped(self):
        """转义引子之后的真注释必须被剥掉。"""
        sql = "SELECT 1 FROM t WHERE c = 'a\\'b' -- create table x\nSELECT 2"
        stripped = vt._strip_sql_comments(sql)
        assert "-- create table x" not in stripped
        assert "SELECT 2" in stripped

    def test_offsets_are_preserved(self):
        """剥注释后长度必须不变（等长空格替换）。"""
        sql = "SELECT 1 FROM t WHERE c = 'a\\'b' -- create table x\nSELECT 2"
        assert len(vt._strip_sql_comments(sql)) == len(sql)

    def test_trailing_comment_is_stripped_with_escaped_quote(self):
        """字符串态必须一路保持到真正的收尾引号。"""
        sql = "SELECT 'a\\'b' -- drop table x"
        stripped = vt._strip_sql_comments(sql)
        assert "-- drop table x" not in stripped


class TestTempTableNameSanitization:
    """临时表名里的每个字符都必须是合法标识符字符。

    ``DATAOPS_EXEC_USER`` 可能是邮箱（``nezha@corp.com``）。早先的
    ``_sanitize_table_name`` 只换 ``.`` 和 ``-``，``@`` 原样进了表名 ——
    既不是合法标识符，又会被清理链路的安全闸门拒掉，临时表永远删不掉。
    """

    def test_at_sign_is_replaced(self):
        name = vt._get_temp_table_name("ads_x", "nezha@corp.com", "20260101", "")
        assert "@" not in name
        assert name == "adhoctemp.tmp_nezha_corp_com_20260101_ads_x"

    def test_sanitized_name_passes_the_safety_gate(self):
        """建表用的名字和清理闸门必须用同一套字符集。"""
        name = vt._get_temp_table_name("ads_x", "nezha@corp.com", "20260101", "")
        assert vt._is_adhoctemp_temp_table(name) is True

    def test_dotted_and_hyphenated_names_still_sanitized(self):
        assert vt._sanitize_table_name("a.b-c") == "a_b_c"


class TestMixedDdlDmlCleanup:
    """一条 SQL 里既有 DDL 又有 DML 时，两张临时表都要删。"""

    MIXED = "CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_x (a INT) PARTITIONED BY (pt_d STRING);\nINSERT OVERWRITE TABLE biads.other_y PARTITION (pt_d = '20260101') SELECT a FROM src;\n"

    def test_extract_returns_every_scratch_table(self):
        names = vt._extract_temp_table_names(self.MIXED, "alice", "", "20260101")
        assert len(names) == 2
        assert any(("ads_x" in name for name in names))
        assert any(("other_y" in name for name in names))
