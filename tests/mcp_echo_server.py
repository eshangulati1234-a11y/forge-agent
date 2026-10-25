"""Minimal MCP server used by test_mcp.py."""
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # mcp >= 2 renamed the high-level server
    from mcp.server.mcpserver import MCPServer as FastMCP

app = FastMCP("echo")


@app.tool()
def shout(text: str, times: int = 1) -> str:
    """Upper-case text, repeated."""
    return " ".join([text.upper()] * times)


if __name__ == "__main__":
    app.run()
