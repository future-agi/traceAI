"""Real mcp-use MCPAgent (scripted LangChain chat model) + stdio MCP media server -> loopback F01."""

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp_use")
pytest.importorskip("traceai_mcp_use")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "frameworks" / "mcp-use" / "tests"))
import _mcp_use_support as support  # noqa: E402  (also turns mcp-use telemetry off)

from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from traceai_mcp_use import FutureAGICallback  # noqa: E402

from _harness import flush, refs  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _mcp_media_server as server  # noqa: E402

SERVER = Path(__file__).resolve().parent / "_mcp_media_server.py"


def run(callback, tool, args=None):
    from mcp_use import MCPAgent, MCPClient

    async def go():
        client = MCPClient.from_dict({"mcpServers": {"media": {"command": sys.executable, "args": [str(SERVER)]}}})
        try:
            agent = MCPAgent(llm=support.ChatFake(script=[support.tool_call(tool, args or {}, "call_media_1"),
                                                          support.answer()]),
                             client=client, callbacks=[callback])
            return await agent.run(support.PROMPT)
        finally:
            await client.close_all_sessions()

    return asyncio.run(go())


def tool_span(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == "execute_tool " + name][-1]


def test_tool_image_audio_and_blob_upload_on_the_tool_span(f01, spans):
    manager = f01.manager()
    provider, exporter = spans
    callback = FutureAGICallback(tracer_provider=provider, attachments=manager)
    assert run(callback, "screenshot") == support.ANSWER
    run(callback, "voice")
    run(callback, "report")
    flush(manager)
    assert sorted((u["mime_type"], u["direction"], u["purpose"], u.get("tool_call_id")) for u in f01.uploads) == [
        ("application/pdf", "output", "tool_result", "call_media_1"),
        ("audio/wav", "output", "tool_result", "call_media_1"),
        ("image/png", "output", "tool_result", "call_media_1")]
    assert server.png(1) in f01.payloads
    span = tool_span(exporter, "screenshot")
    assert len(refs(span)) == 1 and refs(span)[0]["role"] == "tool"
    assert server.b64(server.png(1))[:40] not in str(span.attributes)
    manager.shutdown(5)


def test_provider_manager_is_the_default(f01, spans):
    manager = f01.manager()
    provider, _ = spans
    provider.attachment_manager = manager
    run(FutureAGICallback(tracer_provider=provider), "screenshot")
    flush(manager)
    assert len(f01.uploads) == 1
    manager.shutdown(5)


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_outputs", "pii_redaction"])
def test_local_restrictions_deny(f01, spans, flag):
    manager = f01.manager()
    provider, _ = spans
    before = len(f01.after_bootstrap())
    run(FutureAGICallback(tracer_provider=provider, config=TraceConfig(**{flag: True}), attachments=manager),
        "screenshot")
    flush(manager)
    assert f01.uploads == [] and len(f01.after_bootstrap()) == before
    manager.shutdown(5)


def test_malformed_and_relabeled_media_upload_nothing(f01, spans):
    manager = f01.manager()
    provider, _ = spans
    callback = FutureAGICallback(tracer_provider=provider, attachments=manager)
    run(callback, "relabeled")
    run(callback, "malformed")
    flush(manager)
    assert f01.uploads == []
    manager.shutdown(5)


def test_disabled_manager_uploads_nothing(f01, spans):
    provider, _ = spans
    run(FutureAGICallback(tracer_provider=provider, attachments=create_attachment_manager(config=AttachmentConfig())),
        "screenshot")
    assert f01.uploads == []
