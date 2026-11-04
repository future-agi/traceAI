"""Run examples/agent_with_tools.py with the fake LLM, as its own process.

test_mcp_use_example.py starts this through harness.run. With
``--with-traceai-mcp`` it first instruments the existing transport package
(python/frameworks/mcp, imported, not modified). traceai_mcp wraps
``mcp.client.stdio.stdio_client``; mcp-use binds that name when it is
imported (mcp_use/client/task_managers/stdio.py:12), so the instrumentor
runs before anything imports mcp_use.
"""

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

if "--with-traceai-mcp" in sys.argv:
    sys.path.insert(0, str(HERE.parents[1] / "mcp"))
    from traceai_mcp import MCPInstrumentor

    MCPInstrumentor().instrument()

from _mcp_use_support import ChatFake, answer, tool_call  # noqa: E402

sys.path.insert(0, str(HERE.parent / "examples"))
import agent_with_tools  # noqa: E402

if "--with-traceai-mcp" in sys.argv:
    from mcp_use.client.task_managers import stdio as stdio_task_manager

    # Proof for the test that the transport instrumentation is in place.
    print("traceai_mcp wraps stdio_client:", type(stdio_task_manager.stdio_client).__name__)

script = [tool_call("add", {"a": 2, "b": 3}), answer("EXAMPLE-ANSWER 2 + 3 = 5")]
print("answer:", asyncio.run(agent_with_tools.main(llm=ChatFake(script=script))))
