import asyncio
from contextlib import AsyncExitStack
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

class PlaywrightMCPClient:
    def __init__(self):
        # We use npx to launch the official Playwright MCP server
        self.server_parameters = StdioServerParameters(
            command="npx",
            args=["-y", "@playwright/mcp@latest"]
        )
        self.session = None
        self._exit_stack = AsyncExitStack()

    async def connect(self):
        """Starts the MCP server and establishes the client session."""
        print("[MCP] Starting Playwright MCP Server...")
        stdio_transport = await self._exit_stack.enter_async_context(
            stdio_client(self.server_parameters)
        )
        self.read_stream, self.write_stream = stdio_transport
        
        self.session = await self._exit_stack.enter_async_context(
            ClientSession(self.read_stream, self.write_stream)
        )
        
        await self.session.initialize()
        print("[MCP] Connected successfully.")

    async def execute_tool(self, tool_name: str, arguments: dict):
        """Executes a standardized tool call via the MCP session."""
        if not self.session:
            raise RuntimeError("MCP Client is not connected. Call connect() first.")
        
        print(f"[MCP] Executing {tool_name} with args: {arguments}")
        result = await self.session.call_tool(tool_name, arguments)
        return result

    async def close(self):
        """Cleans up the subprocess and session."""
        await self._exit_stack.aclose()
        print("[MCP] Connection closed.")