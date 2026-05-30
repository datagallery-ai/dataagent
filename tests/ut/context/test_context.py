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
import os

from dataagent.core.context.context_trajectory import ContextFactory

if __name__ == "__main__":
    a = ContextFactory.get_context(user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0)
    a.register_query(query="请分析一下2025年居民的消费信心有没有相较去年发生了什么变化?", additional_files=[])
    a.register_node(
        node_type="Action",
        description="查询与query相关的信息",
        action="Tool(Perceptor)",
        params={"query": "Query(query00000)"},
        output=[
            "Knowledge(居民消费信心)",
            "Knowledge(存款金额计算)",
            "Tool(natural_language_to_sql)",
            "Tool(report_generator)",
            "Table(存款表)",
            "Column(存款表-年份)",
            "Column(存款表-金额)",
            "Column(存款表-性别)",
        ],
        success=True,
        predecessor_node=["Query(query00000)"],
    )
    a.register_node(
        node_type="Knowledge",
        label="存款金额计算",
        description="sql代码，计算某一年的存款金额",
        knowledge_type="calculation",
        knowledge_content="groupby year sum xxx where year = xxx",
        predecessor_node=["Action(action00000)"],
        edge_type="find_relevant_knowledge",
    )
    a.register_node(
        node_type="Knowledge",
        label="居民消费信心",
        description="居民消费信心相关解释",
        knowledge_type="domain",
        knowledge_content="居民的消费信息可以体现在存款方面。当前我国居民仍处于'超额的防御性存款'向'正常存款'的转化。",
        predecessor_node=["Action(action00000)"],
        edge_type="find_relevant_knowledge",
    )
    a.register_node(
        node_type="Tool",
        label="natural_language_to_sql",
        description="Generate and execute SQL script from natural language query.",
        tool_params="""query (str): Natural language query or SQL template.
    data_schema (str): Detailed schema of the source data tables, including their columns,
    column descriptions (as detailed as possible, explicitly mentioning data types such as
    Unix timestamp, DATE, or DATETIME), and the join keys.
    Provide as much detail as possible for each column so that the tool can generate and execute SQL correctly.
    Example:
    table_name_1: [
    { "column_name": "col_name_1", "column_description": "description of col_name_1" },
    { "column_name": "col_name_2", "column_description": "description of col_name_2" }
    ];
    table_name_2: [
    { "column_name": "col_name_1", "column_description": "description of col_name_1" },
    { "column_name": "col_name_2", "column_description": "description of col_name_2" }
    ];
    joins: table_name_1.col_name_1 = table_name_2.col_name_2
    sql_save_path (str): Path to save generated SQL script (with .sql extension).
    csv_save_path (str): Path to save query results as CSV file (with .csv extension).""",
        tool_returns="str: First few lines of execution results.",
        predecessor_node=["Action(action00000)"],
        edge_type="find_relevant_tool",
    )
    a.register_node(
        node_type="Tool",
        label="report_generator",
        description="""
    Based on the provided analysis and images, generate a detailed Markdown-formatted report.
    Running **statistical_analyzer**, **llm_analyzer**, and **natural_language_to_plot** beforehand is recommended.""",
        tool_params="""
    query (str): User's request for the report, which may include specific focus areas or analysis points.
    output_path (str): Output MD file path.
    analysis_path (str): Path to the saved analysis result file.
    images_path (str): JSONL config file path containing image paths and descriptions.""",
        tool_returns="str: Generated Markdown report.",
        predecessor_node=["Action(action00000)"],
        edge_type="find_relevant_tool",
    )
    a.register_node(
        node_type="Table",
        label="存款表",
        description="记录用户存款",
        path="随便编的路径/反正先不测自动推理/存款表.csv",
        predecessor_node=["Action(action00000)"],
        edge_type="find_relevant_data",
    )
    a.register_node(
        node_type="Column",
        label="存款表-金额",
        description="用户存款的具体金额数值，以浮点数类型标识（单位：元）",
        from_table="Table(存款表)",
        values={},
        supplementary_schemas={},
        predecessor_node=["Table(存款表)"],
        edge_type="find_relevant_data",
    )
    a.register_node(
        node_type="Column",
        label="存款表-年份",
        description="用户存款时的年份，以四位整数标识",
        from_table="Table(存款表)",
        values={},
        supplementary_schemas={},
        predecessor_node=["Table(存款表)"],
        edge_type="find_relevant_data",
    )
    a.register_node(
        node_type="Column",
        label="存款表-性别",
        description="用户性别，M表示男性，F表示女性",
        from_table="Table(存款表)",
        values={},
        supplementary_schemas={},
        predecessor_node=["Table(存款表)"],
        edge_type="find_relevant_data",
    )
    a.register_node(
        node_type="State",
        description="完成了perceptor的查询",
        state="查询到与用户query相关的信息，包括存款表中的性别/年龄/金额三列。可用的工具有natural_language_to_sql和\
            report_generator。居民消费信心可以用存款数额来衡量，有一段示例sql语句可以用来计算某一年的存款金额。",
        predecessor_node=["Action(action00000)"],
    )
    a.register_node(
        node_type="Action",
        description="计算当年(2025)与去年(2024)存款总金额",
        action="Tool(natural_language_to_sql)",
        params={
            "query": "计算当年(2025)与去年(2024)存款总金额",
            "table": ["Table(存款表)"],
            "column": ["Column(存款表-金额)", "Column(存款表-年份)"],
            "knowledge": ["Knowledge(存款金额计算)"],
        },
        output=["Script(xxx.sql)", "Table(xxx.csv)"],
        success=True,
        predecessor_node=["State(state00000)"],
    )
    a.register_node(
        node_type="Script",
        label="xxx.sql",
        description="使用nl2sql计算得到2025年与2024年存款金额",
        script_content="select xxx groupby year sum xxx where year = xxx or xxx-1",
        script_type="sql(mysql)",
        path="随便编的路径/后面再说/xxx.sql",
        related_data_list=["Table(存款表)", "Column(存款表-金额)", "Column(存款表-年份)"],
        predecessor_node=["Action(action00001)"],
        edge_type="has_script",
    )
    a.register_node(
        node_type="Table",
        label="xxx.csv",
        description="2025年与2024年存款总金额",
        path="随便编的路径/后面再说/xxx.csv",
        predecessor_node=["Action(action00001)"],
        edge_type="generates",
    )
    a.register_node(
        node_type="State",
        description="计算得到2025年与2024年存款总金额",
        state="2025年相较于2024年，总体存款存在下降",
        predecessor_node=["Action(action00001)"],
    )
    a.register_node(
        node_type="Action",
        description="计算当年(2025)与去年(2024)不同性别用户的存款总金额",
        action="Tool(natural_language_to_sql)",
        params={
            "query": "计算当年(2025)与去年(2024)存款总金额",
            "table": ["Table(存款表)"],
            "column": ["Column(存款表-金额)", "Column(存款表-年份)", "Column(存款表-性别)"],
        },
        output=["Script(yyy.sql)", "Table(yyy.csv)"],
        success=True,
        predecessor_node=["State(state00001)"],
    )
    a.register_node(
        node_type="Script",
        label="yyy.sql",
        description="使用nl2sql计算得到2025年与2024年不同性别存款金额",
        script_content="select xxx groupby year, sex sum xxx where year = xxx or xxx-1",
        script_type="sql(mysql)",
        path="随便编的路径/后面再说/yyy.sql",
        related_data_list=["Table(存款表)", "Column(存款表-金额)", "Column(存款表-年份)", "Column(存款表-性别)"],
        predecessor_node=["Action(action00002)"],
        edge_type="has_script",
    )
    a.register_node(
        node_type="Table",
        label="yyy.csv",
        description="2025年与2024年不同性别存款金额",
        path="随便编的路径/后面再说/yyy.csv",
        predecessor_node=["Action(action00002)"],
        edge_type="generates",
    )
    a.register_node(
        node_type="State",
        description="计算得到2025年与2024年不同性别存款金额",
        state="2025年相较上一年，男性存款发生显著下降，女性存款没有显著变化",
        predecessor_node=["Action(action00002)"],
    )
    a.register_node(
        node_type="Action",
        description="生成报告总结",
        action="Tool(report_generator)",
        params={
            "query": "Query(query00000)",
            "state": ["State(state00001)", "State(state00002)"],
            "table": ["Table(xxx.csv)", "Table(yyy.csv)"],
        },
        output=["File(zzz.html)"],
        success=True,
        predecessor_node=["State(state00002)"],
    )
    a.register_node(
        node_type="File",
        label="zzz.html",
        description="报告总结2025年与上一年存款的变化及原因分析",
        path="随便编的路径/后面再说/zzz.html",
        source="Tool(report_generator)",
        predecessor_node=["Action(action00003)"],
        edge_type="generates",
    )
    a.register_node(
        node_type="State",
        description="完成分析，可以回答用户query了",
        state="2025年居民的总体消费信息增强了，表现在男性的存款减少了，信心增强了；女性没有显著变化。",
        predecessor_node=["Action(action00003)"],
        remove_pt=True,
    )
    a.show(os.path.join(os.path.dirname(__file__), "temp.html"))
