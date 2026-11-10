"""Cancellation must end only its request, not the real MCP transport/session."""
import asyncio
from importlib.metadata import version
import json
from pathlib import Path
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
import mcp.client.stdio as stdio
from mcp.types import (
    CancelledNotification,
    CancelledNotificationParams,
    ClientNotification,
)
import pytest

from traceai_mcp import MCPInstrumentor


@pytest.mark.parametrize("instrumented", [False, True], ids=["upstream", "instrumented"])
def test_cancelled_request_keeps_session_alive(tmp_path, instrumented):
    async def wait_for_marker(name):
        with anyio.fail_after(5):
            while not (tmp_path / name).exists():
                await asyncio.sleep(0.01)

    async def run():
        instrumentor = MCPInstrumentor()
        if instrumented:
            instrumentor.instrument()
            assert instrumentor.is_instrumented_by_opentelemetry
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-I",
                str(Path(__file__).with_name("local_cancel_server.py")),
                str(tmp_path),
                "instrumented" if instrumented else "upstream",
            ],
        )
        try:
            with anyio.fail_after(20):
                async with stdio.stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        first = await session.call_tool("echo", {})
                        assert first.content[0].text == "healthy"
                        pending = asyncio.create_task(session.call_tool("hold", {}))
                        try:
                            await wait_for_marker("started.json")
                            if int(version("mcp").split(".")[0]) >= 2:
                                pending.cancel()
                            else:
                                request_id = json.loads((tmp_path / "started.json").read_text())["id"]
                                await session.send_notification(
                                    ClientNotification(
                                        CancelledNotification(
                                            method="notifications/cancelled",
                                            params=CancelledNotificationParams(
                                                requestId=request_id,
                                                reason="synthetic test cancellation",
                                            ),
                                        )
                                    )
                                )
                            # Prove the server handled cancellation, not merely that
                            # echo beat the notification or the hold finished normally.
                            await wait_for_marker("cancelled")
                            with anyio.fail_after(5):
                                healthy = await session.call_tool("echo", {})
                            assert healthy.content[0].text == "healthy"
                        finally:
                            if not pending.done():
                                pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()

    asyncio.run(run())
