"""Real ``mcp`` ClientSession over stdio -> wrap_mcp_client -> TLS loopback F01 server."""

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp")
traceai_mcp = pytest.importorskip("traceai_mcp")

from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from _harness import PDF, flush, no_bytes, refs, span_named, uploaded  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _mcp_media_server as server  # noqa: E402

SERVER = Path(__file__).resolve().parent / "_mcp_media_server.py"


def run(spans, manager, calls, **wrap):
    """Open a real stdio session, wrap it, and run ``calls(session)``."""
    provider, _ = spans

    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                traced = traceai_mcp.wrap_mcp_client(session, tracer_provider=provider, attachments=manager, **wrap)
                return await calls(traced)

    return asyncio.run(go())


def test_tool_image_audio_and_embedded_blob_upload_with_tool_identity(f01, spans):
    manager = f01.manager()
    _, exporter = spans

    async def calls(session):
        await session.call_tool("screenshot", {})
        await session.call_tool("voice", {})
        result = await session.call_tool("report", {})
        reads = await session.call_tool("reads", {})
        return result, reads

    result, reads = run(spans, manager, calls)
    flush(manager)
    assert sorted(u["mime_type"] for u in f01.uploads) == ["application/pdf", "audio/wav", "image/png"]
    assert {(u["direction"], u["purpose"]) for u in f01.uploads} == {("output", "tool_result")}
    assert server.png(1) in f01.payloads and server.WAV in f01.payloads and PDF in f01.payloads
    # The resource_link URI was never dereferenced.
    assert json.loads(reads.content[0].text)["secret"] == 0
    # The SDK result is unchanged.
    assert result.content[0].resource.blob == server.b64(PDF)
    shot = [s for s in exporter.get_finished_spans() if s.name == "mcp.tools/call"]
    assert len(shot) == 4
    assert sum(len(refs(s)) for s in shot) == 3
    assert {r["role"] for s in shot for r in refs(s)} == {"tool"}
    no_bytes(exporter, server.png(1))
    manager.shutdown(5)


def test_resources_read_blob_uploads_and_text_is_ignored(f01, spans):
    manager = f01.manager()

    async def calls(session):
        return await session.read_resource("file:///shot.png")

    result = run(spans, manager, calls)
    flush(manager)
    assert [u["mime_type"] for u in f01.uploads] == ["image/png"]
    assert f01.payloads == [server.png(5)]
    assert result.contents[0].blob == server.b64(server.png(5))
    manager.shutdown(5)


def test_relabeled_unknown_malformed_and_oversized_media_upload_nothing(f01, spans):
    manager = f01.manager()

    async def calls(session):
        for name in ("relabeled", "malformed", "big"):
            await session.call_tool(name, {})

    run(spans, manager, calls)
    flush(manager)
    assert f01.uploads == []
    manager.shutdown(5)


def test_part_budget_caps_uploads_per_result(f01, spans):
    manager = f01.manager()

    async def calls(session):
        await session.call_tool("many", {"count": 70})

    run(spans, manager, calls)
    flush(manager)
    assert len(f01.uploads) == 64
    manager.shutdown(5)


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_text",
                                  "pii_redaction"])
def test_local_hide_flags_and_pii_deny_every_tool_media(f01, spans, flag):
    manager = f01.manager()
    before = len(f01.after_bootstrap())

    async def calls(session):
        await session.call_tool("screenshot", {})
        await session.call_tool("voice", {})

    run(spans, manager, calls, trace_config=TraceConfig(**{flag: True}))
    flush(manager)
    assert f01.uploads == [] and len(f01.after_bootstrap()) == before
    manager.shutdown(5)


def test_hide_input_images_denies_images_but_not_audio(f01, spans):
    manager = f01.manager()

    async def calls(session):
        await session.call_tool("screenshot", {})
        await session.call_tool("voice", {})

    run(spans, manager, calls, trace_config=TraceConfig(hide_input_images=True))
    flush(manager)
    assert [u["mime_type"] for u in f01.uploads] == ["audio/wav"]
    manager.shutdown(5)


def test_env_hide_flag_denies(f01, spans, monkeypatch):
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    manager = f01.manager()

    async def calls(session):
        await session.call_tool("screenshot", {})

    run(spans, manager, calls)
    flush(manager)
    assert f01.uploads == []
    manager.shutdown(5)


def test_disabled_manager_and_unsampled_span_upload_nothing_and_start_no_threads(f01, spans):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    before = {t.name for t in threading.enumerate()}
    disabled = create_attachment_manager(config=AttachmentConfig())

    async def calls(session):
        return await session.call_tool("screenshot", {})

    result = run(spans, disabled, calls)
    assert result.content[1].data == server.b64(server.png(1))
    assert not {t.name for t in threading.enumerate()} - before
    manager = f01.manager()
    unsampled = TracerProvider(sampler=ALWAYS_OFF)
    run((unsampled, None), manager, calls)
    flush(manager)
    assert f01.uploads == []
    manager.shutdown(5)


def test_provider_manager_is_the_default_and_rewrapping_is_idempotent(f01, spans):
    manager = f01.manager()
    provider, exporter = spans
    provider.attachment_manager = manager

    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                traced = traceai_mcp.wrap_mcp_client(session, tracer_provider=provider)
                assert traceai_mcp.wrap_mcp_client(traced) is traced
                await traced.call_tool("screenshot", {})

    asyncio.run(go())
    flush(manager)
    assert len(f01.uploads) == 1
    assert len([s for s in exporter.get_finished_spans() if s.name == "mcp.tools/call"]) == 1
    manager.shutdown(5)


def test_tool_errors_keep_the_sdk_exception_and_end_the_span(f01, spans):
    manager = f01.manager()
    _, exporter = spans

    async def calls(session):
        with pytest.raises(Exception):
            await session.read_resource("file:///missing.png")

    run(spans, manager, calls)
    span = span_named(exporter, "mcp.resources/read")
    assert span.status.status_code.name == "ERROR"
    manager.shutdown(5)


def test_capture_mcp_result_is_preflight_first_and_never_raises(f01, spans):
    manager = f01.manager()
    provider, _ = spans
    reads = []

    class Part(dict):
        def get(self, key, default=None):
            if key == "data":
                reads.append(key)
            return super().get(key, default)

    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("denied") as span:
        traceai_mcp.capture_mcp_result(manager, span, {"content": [Part(type="image", data="AAAA", mimeType="image/png")]},
                                       trace_config=TraceConfig(hide_outputs=True))
    assert reads == []
    assert traceai_mcp.capture_mcp_result(manager, span, object()) == {}
    assert traceai_mcp.capture_mcp_result(None, span, {"content": []}) == {}
    manager.shutdown(5)
