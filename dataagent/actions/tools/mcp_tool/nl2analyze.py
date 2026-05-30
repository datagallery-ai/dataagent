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
import json
import os
import re

import httpx
import numpy as np
import pandas as pd
from mcp.server.fastmcp import FastMCP

from dataagent.utils.log import setup_subprocess_logging  # noqa: E402

logger = setup_subprocess_logging("nl2analyze")


class AnalyzeServer:
    def __init__(self):
        """
        Initialize the Analyze server.
        """
        self.server = FastMCP("statistical_analyzer")
        self.setup_tools()

    def setup_tools(self):
        """setup tools"""

        @self.server.tool()
        def statistical_analyzer(csv_path: str, json_path: str):
            """Perform statistical analysis based on the task and csv data file.

            Args:
                csv_path (str): Path to the CSV data file. Example: "/data/sales_data.csv".
                json_path (str): Path to save analysis result in JSON format. Example: "/data/analysis_result.json".

            Returns:
                str: Saved analysis file path.
            """
            df = pd.read_csv(csv_path)

            if df is None:
                error_msg = "Failed to read CSV file"
                return json.dumps({"original_msg": error_msg, "frontend_msg": f"错误: {error_msg}"})

            # Get common statistics
            statistics, generated_code = get_common_statistics(df, csv_path)
            logger.trace(f"Generated statistics: {statistics}")

            # Handle error cases from get_common_statistics
            if statistics is None:
                error_msg = generated_code if isinstance(generated_code, str) else "Failed to generate statistics"
                return json.dumps({"original_msg": error_msg, "frontend_msg": f"错误: {error_msg}"})

            def convert(value):
                if isinstance(value, (np.integer,)):
                    return int(value)
                if isinstance(value, (np.floating,)):
                    return float(value)
                if isinstance(value, (np.ndarray,)):
                    return value.tolist()
                return str(value)  # fallback，防止还有其他不可序列化的

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(statistics, f, ensure_ascii=False, indent=4, default=convert)

            res = json.dumps(statistics, ensure_ascii=False, indent=4, default=convert)

            frontend_msg_md = f"""
正在执行 statistical_analyzer 工具生成的代码，代码如下:
```python
{generated_code}
```

statistical_analyzer工具执行完成，执行结果如下:
```json
{res}
```

统计结果已保存到：`{json_path}`
"""
            return json.dumps({"original_msg": json_path, "frontend_msg": frontend_msg_md})

        def generate_statistics_prompt(df, column_types):
            """Generate prompt for LLM to create common statistics code"""
            if df is None or column_types is None:
                return ""

            prompt = f"""Please write Python code to calculate common statistical information for this CSV data.
            The data contains {df.shape[0]} rows and {df.shape[1]} columns.

            Your code must:
            1. Accept a pandas DataFrame named 'df' as input
            2. Calculate standard statistical measures for all columns
            3. Return a dictionary with clear, organized statistical results

            For each column, include appropriate common statistics based on type:
            - Numeric columns: count, missing values, mean, median, min, max,
            standard deviation, variance, sum, 25th percentile, 50th percentile, 75th percentile
            - Categorical columns: count, missing values, unique values count, top category,
            top category count, top category proportion, frequency distribution of all categories
            - Datetime columns: count, missing values, earliest date, latest date, date range,
            most frequent year/month/day (as appropriate), distribution by period
            - String columns: count, missing values, unique values count, shortest length,
            longest length, average length, most common starting/ending characters (if applicable)

            CRITICAL IMPLEMENTATION NOTES:
            - Never use a DataFrame directly in a boolean context (if statements)
            - Use 'if not df.empty:' to check for empty dataframes
            - Handle missing values explicitly in all calculations
            - Round numerical values to 4 decimal places for readability

            Field type information:
            """

            for column, col_type in column_types.items():
                sample_data = df[column].dropna().head(3).tolist()
                prompt += f"- {column} (type: {col_type}): Sample data: {sample_data}\n"

            prompt += """
            The code should return a nested dictionary structured as:
            {
                "dataset_overview": {
                    "total_rows": int,
                    "total_columns": int,
                    "columns_by_type": {
                        "numeric": int,
                        "categorical": int,
                        "datetime": int,
                        "string": int
                    },
                    "missing_values_total": int,
                    "missing_values_percentage": float
                },
                "columns": {
                    "column_name_1": {
                        "type": "numeric/categorical/etc",
                        "statistics": {
                            ... relevant common stats for this column ...
                        }
                    },
                    ... other columns ...
                }
            }

            Return ONLY executable Python code with a function called 'calculate_common_statistics' that takes 'df' as input
            and returns the statistics dictionary. No explanations or extra text.
            """
            return prompt

        def extract_code_from_response(response):
            """Extract Python code from model response"""
            code_match = re.search(r"```python(.*?)```", response, re.DOTALL)
            if code_match:
                return code_match.group(1).strip()
            return response

        def analyze_column_types(df):
            """Analyze data types of each column in the DataFrame"""
            if df is None:
                return None

            column_types = {}
            for column in df.columns:
                dtype = str(df[column].dtype)
                if "int" in dtype or "float" in dtype:
                    column_types[column] = "numeric"
                elif "datetime" in dtype:
                    column_types[column] = "datetime"
                else:
                    unique_ratio = len(df[column].unique()) / len(df[column]) if len(df[column]) > 0 else 0
                    if unique_ratio < 0.3 and df[column].dtype == "object":
                        column_types[column] = "categorical"
                    else:
                        column_types[column] = "string"

            return column_types

        def execute_generated_code(code, df):
            """Execute the generated code and return statistics"""
            try:
                exec_globals = {"df": df, "pd": pd}
                exec(code, exec_globals)

                # Look for the required function
                if "calculate_common_statistics" in exec_globals and callable(
                    exec_globals["calculate_common_statistics"]
                ):
                    return exec_globals["calculate_common_statistics"](df)

                raise Exception("Generated code does not contain a 'calculate_common_statistics' function")
            except ValueError as e:
                if "The truth value of a DataFrame is ambiguous" in str(e):
                    logger.trace(f"Error: Improper DataFrame boolean check - {str(e)}")
                    logger.trace("Use 'if not df.empty:' instead of checking the DataFrame directly")
                else:
                    logger.trace(f"Value error: {str(e)}")
                logger.trace("Generated code:")
                logger.trace(code)
                return None
            except Exception as e:
                logger.trace(f"Error executing code: {str(e)}")
                logger.trace("Generated code:")
                logger.trace(code)
                return None

        def get_common_statistics(df, file_path):
            """Generate common statistics for the CSV file"""
            # Analyze column types
            column_types = analyze_column_types(df)
            if not column_types:
                return None, None

            # Generate prompt focused on common statistics
            prompt = generate_statistics_prompt(df, column_types)

            # Get statistics code from LLM
            try:
                logger.debug("Generating common statistics calculation code...")

                # 允许通过环境变量覆盖默认的 LLM 配置，未配置时，仍然使用 deepseek-chat 作为默认模型。
                llm_model = os.getenv("NL2ANALYZE_LLM_MODEL", "deepseek-chat")
                llm_provider = os.getenv("NL2ANALYZE_LLM_PROVIDER", "deepseek")
                llm_base_url = os.getenv("NL2ANALYZE_LLM_BASE_URL")
                llm_api_key = os.getenv("NL2ANALYZE_LLM_API_KEY", "")

                # 组装给 init_chat_model 的参数
                llm_kwargs = {
                    "model": llm_model,
                    "model_provider": llm_provider,
                }
                logger.debug(f"MCP tool llm config: model={llm_model}, provider={llm_provider}")
                if llm_base_url:
                    llm_kwargs["base_url"] = llm_base_url
                    logger.debug(f"MCP tool llm config: base_url={llm_base_url}")
                if llm_api_key:
                    llm_kwargs["api_key"] = llm_api_key
                    logger.debug(f"MCP tool llm config: api_key={llm_api_key[:2]}...{llm_api_key[-2:]}")

                deploy_zone = os.getenv("DEPLOY_ZONE", "")
                if deploy_zone == "internal":
                    proxy = os.getenv("HTTP_PROXY")
                    if not proxy:
                        logger.warning("黄区未配置代理，将无法访问外部模型")
                    os.environ["HTTP_PROXY"] = proxy
                    os.environ["HTTPS_PROXY"] = proxy
                    http_client = httpx.Client(proxy=proxy, verify=False)
                    from langchain.chat_models import init_chat_model  # type: ignore[import-not-found]
                    from langchain.schema import HumanMessage  # type: ignore[import-not-found]

                    # internal 场景下额外传入 http_client
                    llm = init_chat_model(http_client=http_client, **llm_kwargs)  # type: ignore[arg-type]
                else:
                    from langchain.chat_models import init_chat_model  # type: ignore[import-not-found]
                    from langchain.schema import HumanMessage  # type: ignore[import-not-found]

                    llm = init_chat_model(**llm_kwargs)  # type: ignore[arg-type]
                response = llm([HumanMessage(content=prompt)])
                generated_code = extract_code_from_response(response.content)

                # Save generated code
                code_file = f"{file_path.split('.')[0]}_common_stats_code.py"
                with open(code_file, "w", encoding="utf-8") as f:
                    f.write(generated_code)
                logger.debug(f"Statistics code saved to {code_file}")

                # Execute code to get statistics
                logger.debug("Calculating common statistics...")
                statistics = execute_generated_code(generated_code, df)

                if statistics is not None:
                    # Save statistics results
                    output_file = f"{file_path.split('.')[0]}_common_statistics.txt"
                    with open(output_file, "w", encoding="utf-8") as f:
                        f.write(format_statistics(statistics))
                    logger.debug(f"Common statistics saved to {output_file}")

                return statistics, generated_code
            except Exception as e:
                logger.error(f"Error during statistics generation: {str(e)}")
                return None, f"Error during statistics generation: {str(e)}"

        def format_statistics(statistics):
            """Format statistics for readability"""
            result_str = "=== Common Statistical Analysis Results ===\n\n"

            # Dataset overview
            if "dataset_overview" in statistics:
                result_str += "Dataset Overview:\n"
                result_str += "-----------------\n"
                for key, value in statistics["dataset_overview"].items():
                    if key == "columns_by_type":
                        result_str += "  Columns by type:\n"
                        for type_name, count in value.items():
                            result_str += f"    - {type_name}: {count}\n"
                    else:
                        result_str += f"{key}: {value:.2f}" if isinstance(value, float) else f"{key}: {value}\n"
                result_str += "\n"

            # Column-specific statistics
            if "columns" in statistics:
                result_str += "Column Statistics:\n"
                result_str += "------------------\n"
                for column, data in statistics["columns"].items():
                    result_str += f"\n{column} (Type: {data.get('type', 'unknown')}):\n"
                    for stat, value in data.get("statistics", {}).items():
                        if isinstance(value, dict) and "frequency" in stat.lower():
                            result_str += f"  - {stat}:\n"
                            for item, count in value.items():
                                result_str += f"    * {item}: {count}\n"
                        else:
                            result_str += (
                                f"  - {stat}: {value:.4f}" if isinstance(value, float) else f"  - {stat}: {value}\n"
                            )

            return result_str


def main():
    """Main entry point for the server."""

    analyze = AnalyzeServer()

    analyze.server.run(transport="stdio")


if __name__ == "__main__":
    main()
