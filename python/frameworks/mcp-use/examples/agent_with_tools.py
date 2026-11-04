"""Trace one mcp-use agent run with traceAI-mcp-use.

Needs FI_API_KEY / FI_SECRET_KEY, langchain-openai and OPENAI_API_KEY (or
pass another LangChain chat model to main()). The MCP server is the local
stdio server next to this file.
"""

import asyncio
import os
import sys

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from mcp_use import MCPAgent, MCPClient

from traceai_mcp_use import FutureAGICallback

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calculator_server.py")


def default_llm():
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model="gpt-4o-mini")


async def main(llm=None) -> str:
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="mcp-use-agent",
    )
    client = MCPClient.from_dict(
        {"mcpServers": {"calculator": {"command": sys.executable, "args": [SERVER]}}}
    )
    agent = MCPAgent(
        llm=llm or default_llm(),
        client=client,
        callbacks=[FutureAGICallback(tracer_provider=trace_provider)],
    )
    try:
        return await agent.run("What is 2 + 3? Use the add tool.")
    finally:
        await client.close_all_sessions()


if __name__ == "__main__":
    print(asyncio.run(main()))
