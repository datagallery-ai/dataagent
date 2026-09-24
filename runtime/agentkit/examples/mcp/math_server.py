"""Minimal local MCP service. Run with the DataAgent Python environment."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("math")


@server.tool()
def add(a: float, b: float) -> float:
    """Add two numbers and return their sum."""
    return a + b


if __name__ == "__main__":
    server.run(transport="stdio")
