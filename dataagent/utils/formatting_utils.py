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
import textwrap
from collections.abc import Mapping, Sequence
from typing import Any

from loguru import logger
from matplotlib import font_manager

from dataagent.utils.parsing_utils import extract_json_block, remove_think_block

SENSITIVE_URI_PATTERN = re.compile(
    r"(?P<scheme>[a-zA-Z][\w+.-]*://)"
    r"(?P<credentials>[^@/\s]+)@"
    r"(?P<host>[^:/\s]+)"
    r"(?::(?P<port>\d+))?"
)


def format_llm_output(llm_output: str, separator_token: str = "</think>") -> Any:
    """
    Format LLM output by removing think block and extracting JSON

    Args:
        llm_output (str): Raw output from LLM
        separator_token (str): Token to separate think block from actual response

    Returns:
        Any: Formatted LLM output as parsed JSON object
    """

    def _normalize_parsed(obj: Any) -> Any:
        """
        对 LLM 输出做轻量归一化，避免因模型偶发漏字段导致工作流直接崩溃。
        注意：只在字段缺失时补默认值，不改已有字段语义。
        """
        if isinstance(obj, dict):
            # planner: plan/task_status 兼容（模型偶发漏 task_status）
            if isinstance(obj.get("plan"), list):
                for t in obj["plan"]:
                    if isinstance(t, dict) and "task_status" not in t:
                        t["task_status"] = "ADD"
            return obj
        return obj

    llm_output = remove_think_block(llm_output, separator_token)
    formatted_llm_output = extract_json_block(llm_output)
    return _normalize_parsed(formatted_llm_output)


def json_to_markdown(json_str, indent=0):
    """
    Convert JSON string to Markdown format

    Args:
        json_str (str): JSON string, can include or exclude ```json``` markers
        indent (int): Initial indentation level

    Returns:
        str: Formatted Markdown string
    """
    # Remove possible ```json``` markers
    json_str = re.sub(r"```json\s*", "", json_str)
    json_str = re.sub(r"\s*```", "", json_str)

    try:
        # Parse JSON
        data = json.loads(json_str)
        return _format_data(data, indent)
    except json.JSONDecodeError as e:
        return f"JSON parsing error: {str(e)}"


def _format_data(data, indent=0):
    """
    Recursively format data to Markdown (private method)

    Args:
        data: Data to format (dict, list, string, etc.)
        indent (int): Current indentation level

    Returns:
        str: Formatted Markdown string
    """
    if isinstance(data, dict):
        result = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                result.append(f"{'  ' * indent}- **{key}**")
                result.append(_format_data(value, indent + 1))
            else:
                display_value = "None" if value is None else str(value)
                result.append(f"{'  ' * indent}- **{key}**")
                result.append(f"{'  ' * (indent + 1)}{display_value}")
        return "\n".join(result)

    if isinstance(data, list):
        result = []
        for i, item in enumerate(data):
            if isinstance(item, (dict, list)):
                result.append(f"{'  ' * indent}- **[{i}]**")
                result.append(_format_data(item, indent + 1))
            else:
                display_value = "None" if item is None else str(item)
                result.append(f"{'  ' * indent}- **[{i}]**")
                result.append(f"{'  ' * (indent + 1)}{display_value}")
        return "\n".join(result)
    display_value = "None" if data is None else str(data)
    return f"{'  ' * indent}{display_value}"


def filter_json_serializable(obj: Any) -> Any:
    """
    Filter out non-JSON serializable objects

    Args:
        obj (Any): Object to filter

    Returns:
        Any: Filtered object that is JSON serializable
    """
    if isinstance(obj, dict):
        return {
            k: filter_json_serializable(v) for k, v in obj.items() if not callable(v) and not hasattr(v, "__dict__")
        }
    if isinstance(obj, list):
        return [filter_json_serializable(item) for item in obj if not callable(item) and not hasattr(item, "__dict__")]
    if callable(obj) or hasattr(obj, "__dict__"):
        return str(obj)
    return obj


def add_prefix_to_md_images(md_content, prefix_url):
    """
    给Markdown字符串中的图片链接添加前缀

    Args:
        md_content (str): 输入的Markdown字符串
        prefix_url (str): 要添加的前缀URL

    Returns:
        str: 处理后的Markdown字符串

    Example:
        >>> md_content = "![描述](images/chart.png)"
        >>> prefix_url = "http://8.92.9.183/static/"
        >>> result = add_prefix_to_md_images(md_content, prefix_url)
        >>> print(result)
        ![描述](http://8.92.9.183/static/images/chart.png)
    """

    # 使用正则表达式匹配Markdown图片语法
    # 匹配格式: ![alt文本](图片路径)
    pattern = r"!\[([^\]]*)\]\(([^)]+)\)"

    def replace_image(match):
        alt_text = match.group(1)  # 图片描述文本
        image_path = match.group(2)  # 图片路径

        # 如果图片路径已经是完整URL，则不添加前缀
        if image_path.startswith(("http://", "https://", "//")):
            return f"![{alt_text}]({image_path})"

        # 添加前缀到图片路径
        # 如果前缀不以/结尾，且图片路径不以/开头，则添加/
        if not prefix_url.endswith("/") and not image_path.startswith("/"):
            full_url = f"{prefix_url}/{image_path}"
        else:
            full_url = f"{prefix_url}{image_path}"

        return f"![{alt_text}]({full_url})"

    # 执行替换
    result = re.sub(pattern, replace_image, md_content)

    return result


def _detect_chinese_font() -> tuple[str, str]:
    """
    内部函数：检测系统中可用的中文字体。

    Returns:
        tuple[str, str]: (font_name, font_path)
    """
    try:
        import subprocess

        result = subprocess.run(
            ["/usr/bin/fc-list", ":lang=zh", "-f", "%{family[0]}|%{file}\n"], capture_output=True, text=True, timeout=10
        )

        if result.returncode != 0 or not result.stdout.strip():
            logger.warning("fc-list failed, falling back to default font")
            return "SimHei", ""

        chinese_fonts_with_path = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) == 2:
                font_name = parts[0].strip()
                font_path = parts[1].strip()
                if font_path and os.path.exists(font_path):
                    chinese_fonts_with_path.append((font_name, font_path))

        if not chinese_fonts_with_path:
            logger.warning("No Chinese fonts found via fc-list")
            return "SimHei", ""

        def font_priority(item: tuple[str, str]) -> int:
            name = item[0].lower()
            if "zen hei" in name:
                return 1
            if "micro hei" in name:
                return 2
            return 3

        chinese_fonts_with_path.sort(key=font_priority)
        selected_font_name, selected_font_path = chinese_fonts_with_path[0]

        try:
            font_manager.fontManager.addfont(selected_font_path)
        except Exception as e:
            logger.debug(f"Font registration note: {e}")

        return selected_font_name, selected_font_path

    except Exception as e:
        logger.debug(f"Error detecting Chinese font path: {e}")
        return "SimHei", ""


def get_available_chinese_font_path(runtime: Any = None) -> tuple[str, str]:
    """
    Get the best available Chinese font for matplotlib visualization using absolute path.
    结果会缓存到 runtime._cache 中。

    Args:
        runtime: Agent runtime instance for caching

    Returns:
        tuple[str, str]: (font_name, font_path) - The font family name and absolute path
    """
    cache_key = "chinese_font"
    if runtime is not None:
        cached = runtime.get_cache(cache_key)
        if cached is not None:
            return cached
        result = _detect_chinese_font()
        runtime.set_cache(cache_key, result)
        logger.debug(f"Chinese font cached in runtime: {result}")
        return result
    return _detect_chinese_font()


def get_available_chinese_font():
    """
    Get the best available Chinese font for matplotlib visualization.

    Priority:
    1. Use user configured fonts if available in system
    2. Find any available Chinese font in system
    3. Fallback to default font

    Returns:
        str: The best available Chinese font name
    """
    try:
        # get all available fonts in system
        available_fonts = [f.name for f in font_manager.fontManager.ttflist]

        # get user configured chinese fonts
        user_chinese_fonts = [
            "SimHei",
            "Microsoft YaHei",
            "PingFang SC",
            "WenQuanYi Zen Hei",
            "Noto Sans CJK SC",
            "Source Han Sans SC",
        ]

        # use user configured fonts if available
        for font in user_chinese_fonts:
            if font in available_fonts:
                logger.trace(f"Using configured Chinese font: {font}")
                return font

        # if user configured fonts are not available, find common chinese fonts
        common_chinese_fonts = [
            "SimHei",
            "黑体",
            "Microsoft YaHei",
            "微软雅黑",
            "PingFang SC",
            "苹方",
            "Hiragino Sans GB",
            "冬青黑体简体中文",
            "WenQuanYi Zen Hei",
            "文泉驿正黑",
            "WenQuanYi Micro Hei",
            "文泉驿微米黑",
            "Noto Sans CJK SC",
            "Noto Sans CJK",
            "Source Han Sans SC",
            "思源黑体",
            "STXihei",
            "华文细黑",
            "STKaiti",
            "华文楷体",
            "STSong",
            "华文宋体",
            "Droid Sans Fallback",
            "AR PL UMing CN",
            "AR PL UKai CN",
        ]

        # find available chinese fonts in system
        for font in common_chinese_fonts:
            if font in available_fonts:
                logger.trace(f"Using system Chinese font: {font}")
                return font

        # try to find any font contains chinese characters
        font_keywords = [
            "chinese",
            "cjk",
            "han",
            "zh",
            "cn",
            "simhei",
            "yahei",
            "pingfang",
            "wenquanyi",
            "noto",
            "source",
            "droid",
        ]

        chinese_pattern_fonts = [
            font for font in available_fonts if any(keyword in font.lower() for keyword in font_keywords)
        ]

        if chinese_pattern_fonts:
            selected_font = chinese_pattern_fonts[0]
            logger.trace(f"Using detected Chinese font: {selected_font}")
            return selected_font

        # if no chinese fonts found, use default font and record warning
        default_font = user_chinese_fonts[0] if user_chinese_fonts else "SimHei"
        logger.warning(f"No Chinese fonts found in system. Using default: {default_font}")
        logger.warning(f"Available fonts: {len(available_fonts)} total")
        return default_font

    except Exception as e:
        logger.error(f"Error detecting Chinese fonts: {e}")
        return "SimHei"


def wrap_print(text: str, width: int = 80, indent: str = "") -> None:
    """Wraps and prints text to the console with smart handling for ASCII tree structures
    and bullet points.

    This function iterates through lines of text and applies context-aware wrapping:
    1. Regular text is wrapped to the specified width with indentation.
    2. Bullet points and tree branches (├─, └─) are preserved to avoid formatting issues.
    3. Vertical tree lines (│) are handled specially: the content is wrapped, but the
       vertical structural prefix is repeated for every wrapped line to maintain
       visual continuity.

    Args:
        text (str): The input string to wrap and print.
        width (int): The maximum character width for the output lines. Defaults to 80.
        indent (str): A string to prepend to regular text lines. Defaults to "".
    """
    if not text:
        print()
        return
    # Avoid textwrap errors when callers pass very narrow widths.
    width = max(1, width)

    lines = text.split("\n")
    wrapped_lines = []

    for line in lines:
        if line.strip():
            if line.strip().startswith(("•", "-", "*", "├─", "└─")):
                # Keep bullet points on their own line
                wrapped_lines.append(line)
            elif line.strip().startswith("│"):
                # Handle tree structure lines - wrap them properly
                if len(line) > width:
                    # Find the content after the tree structure
                    content_start = line.find("•")
                    if content_start == -1:
                        content_start = line.find("└─")
                        if content_start == -1:
                            content_start = line.find("├─")

                    if content_start != -1:
                        prefix = line[:content_start]
                        content = line[content_start:]
                        content_width = width - len(prefix)
                        if content_width <= 0:
                            # Keep tree lines intact when their prefix is wider than the target width.
                            wrapped_lines.append(line)
                            continue

                        # Wrap the content part
                        wrapped_content = textwrap.fill(
                            content, width=content_width, initial_indent="", subsequent_indent=""
                        )

                        # Split wrapped content and add prefix to each line
                        content_lines = wrapped_content.split("\n")
                        for i, content_line in enumerate(content_lines):
                            if i == 0:
                                wrapped_lines.append(prefix + content_line)
                            else:
                                # Ensure wrapped lines maintain the │ character
                                wrapped_lines.append(prefix + content_line)
                    else:
                        wrapped_lines.append(line)
                else:
                    wrapped_lines.append(line)
            else:
                # Regular text wrapping
                wrapped = textwrap.fill(line, width=width, initial_indent=indent, subsequent_indent=indent)
                wrapped_lines.append(wrapped)
        else:
            wrapped_lines.append(line)

    for line in wrapped_lines:
        print(line)


def format_revision_instructions_to_markdown(revision_instructions):
    """将修改意见字典格式转换为markdown格式"""
    if not revision_instructions or not isinstance(revision_instructions, dict):
        return "无修改意见"

    markdown_content = []
    for task_id, task_info in revision_instructions.items():
        operation = task_info.get("operation", "UNKNOWN")
        instructions = task_info.get("instructions", "无具体说明")

        # 根据操作类型设置不同的标识符
        if operation == "UPDATE":
            status = "修改"
        elif operation == "UNCHANGE":
            continue
        elif operation == "DELETE":
            status = "删除"
        elif operation == "CREATE":
            status = "新建"
        else:
            status = operation

        markdown_content.append(f"- {status}任务{task_id}：")
        markdown_content.append(f"\n{instructions}\n")

    return "\n".join(markdown_content)


def truncate_tool_args(tool_args: Any, max_length: int = 400, max_lines: int = 10) -> str:
    """将工具参数序列化为多行文本，过长时截断。"""
    try:
        args_text = json.dumps(tool_args, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        args_text = str(tool_args)
    if len(args_text) > max_length:
        args_text = f"{args_text[: max_length - 3]}..."
    lines = args_text.splitlines()
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], "..."]
    return "\n".join(lines)


def format_tool_calls_for_display(tool_calls: Sequence[Mapping[str, Any]]) -> str:
    """格式化工具调用展示内容，包含截断后的参数。"""
    formatted_blocks: list[str] = []
    for tool_call in tool_calls:
        tool_name = str(tool_call["name"])
        args_text = truncate_tool_args(tool_call.get("args", {}))
        indented_args = textwrap.indent(args_text, "    ")
        formatted_blocks.append(f"- **{tool_name}**\n  - args:\n{indented_args}")
    return "\n".join(formatted_blocks)


def save_user_query_to_file(file_save_path: str, user_query: str) -> None:
    """将用户查询保存到指定目录下的prompt.txt文件中。"""
    prompt_file_path = os.path.join(file_save_path, "prompt.txt")
    with open(prompt_file_path, "w", encoding="utf-8") as f:
        f.write(user_query)
    logger.debug(f"已保存对话到: {prompt_file_path}")


def mask_sensitive_connection_info(text: str) -> str:
    """
    Mask usernames and passwords in connection strings while keeping URL endpoints visible.

    Example:
        mysql+pymysql://<username>:<password>@<host>:3306/db
        ->
        mysql+pymysql://***:***@<host>:3306/db
    """

    def _replace(match: re.Match) -> str:
        scheme = match.group("scheme")
        credentials = match.group("credentials")
        host = match.group("host")
        port = match.group("port")
        has_password = ":" in credentials
        masked_credentials = "***:***" if has_password else "***"
        visible_port = f":{port}" if port else ""
        return f"{scheme}{masked_credentials}@{host}{visible_port}"

    return SENSITIVE_URI_PATTERN.sub(_replace, text)
