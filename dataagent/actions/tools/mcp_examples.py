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
import asyncio
import os

from loguru import logger
from mcp.server.fastmcp import FastMCP

from dataagent.actions.tools.mcp import MCPToolRegistry


# 示例1: 创建FastMCP服务器并托管 DataAgent 工具
def create_dataagent_mcp_server():
    """创建一个托管 DataAgent 工具的FastMCP服务器"""

    # 创建FastMCP服务器实例
    server = FastMCP("DataAgent Tools Server")

    # 定义一些示例工具函数
    @server.tool()
    def calculate_sum(a: int, b: int) -> int:
        """计算两个数的和"""
        return a + b

    @server.tool()
    def get_file_info(file_path: str) -> str:
        """获取文件信息"""
        try:
            if os.path.exists(file_path):
                size = os.path.getsize(file_path)
                return f"File: {file_path}, Size: {size} bytes"
            return f"File not found: {file_path}"
        except Exception as e:
            return f"Error: {str(e)}"

    @server.tool()
    async def list_directory(path: str) -> list[str]:
        """列出目录内容"""
        try:
            if os.path.isdir(path):
                return os.listdir(path)
            return [f"Not a directory: {path}"]
        except Exception as e:
            return [f"Error: {str(e)}"]

    return server


# 示例2: 使用装饰器创建MCP工具
def create_decorated_mcp_server():
    """使用装饰器语法创建MCP服务器"""

    server = FastMCP("Decorated Tools Server")

    @server.mcp.tool()
    def weather_info(city: str) -> str:
        """获取城市天气信息（模拟）"""
        weather_data = {
            "beijing": "晴天, 25°C",
            "shanghai": "多云, 28°C",
            "guangzhou": "雨天, 30°C",
            "shenzhen": "晴天, 32°C",
        }
        return weather_data.get(city.lower(), f"未知城市: {city}")

    @server.mcp.tool()
    def translate_text(text: str, target_lang: str = "en") -> str:
        """翻译文本（模拟）"""
        translations = {
            "hello": {"zh": "你好", "fr": "bonjour", "es": "hola"},
            "world": {"zh": "世界", "fr": "monde", "es": "mundo"},
            "thank you": {"zh": "谢谢", "fr": "merci", "es": "gracias"},
        }

        if text.lower() in translations and target_lang in translations[text.lower()]:
            return translations[text.lower()][target_lang]
        return f"Translation not available for '{text}' to {target_lang}"

    return server


# 示例3: MCP客户端连接示例
async def connect_to_mcp_server():
    """连接到MCP服务器并使用工具"""

    # 创建注册表
    registry = MCPToolRegistry()

    # 注册一个MCP服务器（假设有一个外部MCP服务器）
    # 这里使用python -m示例，实际使用时替换为真实的MCP服务器命令

    registry.register_server(
        server_id="filesystem_server",
        transport_type="stdio",
        config={
            "command": "python",
            "args": ["-m", "dataagent.actions.tools.mcp_examples", "--server", "filesystem"],
            "env": {},
        },
    )

    try:
        # 发现工具
        tools = await registry.list_server_tools("filesystem_server")
        logger.trace(f"发现的工具: {[tool.name for tool in tools]}")

        # 调用工具
        for tool in tools:
            logger.trace(f"\n工具: {tool.name}")
            logger.trace(f"描述: {tool.description}")

            # 示例调用（根据实际工具调整参数）
            if "calculate_sum" in tool.name:
                result = tool.call(a=10, b=20)
                logger.trace(f"结果: {result.data if result.success else result.error}")
            elif "get_file_info" in tool.name:
                result = tool.call(file_path="/tmp")
                logger.trace(f"结果: {result.data if result.success else result.error}")

    except Exception as e:
        logger.debug(f"MCP客户端示例错误: {e}")
    finally:
        await registry.cleanup()


# 示例4: 运行FastMCP服务器
def run_stdio_server():
    """运行stdio模式的FastMCP服务器"""
    server = create_dataagent_mcp_server()
    logger.debug("启动 DataAgent MCP服务器 (stdio模式)...")
    server.run(transport="stdio")


def run_sse_server():
    """运行SSE模式的FastMCP服务器"""
    server = create_decorated_mcp_server()
    logger.debug("启动装饰器MCP服务器 (SSE模式) on http://localhost:8000...")
    server.run(transport="sse", host="localhost", port=8000)


# 示例5: 集成到 DataAgent 工具管理器
async def integrate_with_dataagent():
    """演示如何将MCP集成到 DataAgent 工具管理器"""
    from dataagent.core.managers.action_manager.manager import ToolManager

    tool_manager = ToolManager()
    try:
        # 注册MCP服务器到 DataAgent 工具管理器
        tool_manager.register_mcp_server(
            server_id="demo_server",
            transport_type="stdio",
            config={"command": "python", "args": ["-m", "dataagent.actions.tools.mcp_examples", "--server", "demo"]},
        )

        # 发现并注册工具
        discovered_tools = await tool_manager.discover_mcp_tools("demo_server")
        logger.debug(f"集成到 DataAgent 的MCP工具: {discovered_tools}")

        # 通过 DataAgent 调用MCP工具
        if "demo_server.calculate_sum" in discovered_tools:
            result = tool_manager.call("demo_server.calculate_sum", a=5, b=10)
            logger.trace(f"通过 DataAgent 调用MCP工具结果: {result.data}")

    except Exception as e:
        logger.debug(f"DataAgent 集成示例错误: {e}")


# 命令行接口
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "--server":
            server_type = sys.argv[2] if len(sys.argv) > 2 else "filesystem"

            if server_type == "filesystem" or server_type == "demo":
                run_stdio_server()
            elif server_type == "sse":
                run_sse_server()
            else:
                logger.debug(f"未知服务器类型: {server_type}")

        elif command == "--client":
            asyncio.run(connect_to_mcp_server())

        elif command == "--dataagent":
            asyncio.run(integrate_with_dataagent())

    else:
        logger.debug("""
MCP集成示例用法:

服务器模式:
  python -m dataagent.actions.tools.mcp_examples --server filesystem  # stdio模式
  python -m dataagent.actions.tools.mcp_examples --server sse        # SSE模式

客户端模式:
  python -m dataagent.actions.tools.mcp_examples --client

DataAgent 集成:
  python -m dataagent.actions.tools.mcp_examples --dataagent

单独测试:
  python dataagent/actions/tools/mcp_examples.py
        """)

        # 运行默认示例
        logger.debug("\n=== 创建FastMCP服务器示例 ===")
        server1 = create_dataagent_mcp_server()
        logger.debug(f"创建的服务器: {server1.name}")
        logger.trace(f"注册的工具: {[tool.name for tool in server1.get_tools()]}")

        logger.debug("\n=== 装饰器MCP服务器示例 ===")
        server2 = create_decorated_mcp_server()
        logger.debug(f"创建的服务器: {server2.name}")
        logger.trace(f"注册的工具: {[tool.name for tool in server2.get_tools()]}")

        logger.debug("\n要运行实际的MCP服务器，请使用 --server 参数")
        logger.debug("要测试MCP客户端，请使用 --client 参数")
