"""Real claude_agent_sdk ClaudeSDKClient over its subprocess transport -> loopback F01.

The CLI is ``_fake_claude_cli.py`` (the SDK's real stream-json control protocol:
initialize, hook callbacks, user/assistant/result messages); no model or key.
"""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

claude_agent_sdk = pytest.importorskip("claude_agent_sdk")
pytest.importorskip("traceai_claude_agent_sdk")

from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from traceai_claude_agent_sdk import ClaudeAgentInstrumentor  # noqa: E402
from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes  # noqa: E402

from _harness import PDF, b64, flush, png, refs  # noqa: E402

CLI = Path(__file__).resolve().parent / "_fake_claude_cli.py"
SHOT = png(seed=51)


def image(data, mime="image/png"):
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64(data)}}


@pytest.fixture
def cli(tmp_path, monkeypatch):
    def make(content, order="hooks-first", is_error=False, gap=0.0):
        result = tmp_path / "result.json"
        result.write_text(json.dumps({"content": content, "is_error": is_error}))
        launcher = tmp_path / "claude"
        launcher.write_text("#!%s\nimport runpy\nrunpy.run_path(%r, run_name='__main__')\n" % (sys.executable, str(CLI)))
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("FAKE_CLAUDE_RESULT", str(result))
        monkeypatch.setenv("FAKE_CLAUDE_ORDER", order)
        monkeypatch.setenv("FAKE_CLAUDE_GAP", str(gap))
        monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
        # Releases without ClaudeAgentOptions.cli_path find the CLI as "claude" on PATH.
        monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
        return str(launcher)

    return make


@pytest.fixture
def instrument(spans):
    provider, exporter = spans
    state = {}

    def install(manager, config=None):
        instrumentor = ClaudeAgentInstrumentor()
        instrumentor.instrument(tracer_provider=provider, attachments=manager, config=config or TraceConfig(),
                                skip_dep_check=True)
        state["instrumentor"] = instrumentor
        return exporter

    yield install
    if "instrumentor" in state:
        state["instrumentor"].uninstrument()


def converse(cli_path):
    async def go():
        fields = getattr(claude_agent_sdk.ClaudeAgentOptions, "__dataclass_fields__", {})
        options = claude_agent_sdk.ClaudeAgentOptions(**({"cli_path": cli_path} if "cli_path" in fields else {}))
        messages = []
        async with claude_agent_sdk.ClaudeSDKClient(options=options) as client:
            await client.query("take a screenshot")
            async for message in client.receive_response():
                messages.append(message)
        return messages

    return asyncio.run(go())


def tool_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.name.startswith("tool.")]


@pytest.mark.parametrize("order", ["hooks-first", "result-first"])
def test_tool_result_image_and_pdf_upload_before_truncation_on_the_tool_span(f01, instrument, cli, order):
    manager = f01.manager()
    exporter = instrument(manager)
    messages = converse(cli([{"type": "text", "text": "captured"}, image(SHOT),
                             {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                             "data": b64(PDF)}}], order=order))
    flush(manager)
    assert type(messages[-1]).__name__ == "ResultMessage"
    assert sorted((u["mime_type"], u["direction"], u["purpose"], u.get("tool_call_id")) for u in f01.uploads) == [
        ("application/pdf", "output", "tool_result", "toolu_screen_1"),
        ("image/png", "output", "tool_result", "toolu_screen_1")]
    assert sorted(f01.payloads) == sorted([SHOT, PDF])
    (tool,) = tool_spans(exporter)
    parents = {u["association"]["parent"]["span_id"] for u in f01.uploads}
    assert parents == {format(tool.context.span_id, "016x")}
    # In both orders the tool span is still recording when its result arrives, so every
    # uploaded occurrence is projected (fi.attachment.refs) on its upload parent.
    assert sorted(r["occurrence_id"] for r in refs(tool)) == sorted(u["occurrence_id"] for u in f01.uploads)
    assert tool.status.is_ok
    # Bytes never reach a text attribute (tool output, conversation output).
    for span in exporter.get_finished_spans():
        for value in span.attributes.values():
            assert b64(SHOT)[:40] not in str(value)
    manager.shutdown(5)


def _duration_ms(span):
    return (span.end_time - span.start_time) / 1e6


def test_hooks_first_tool_span_keeps_the_hook_end_time_while_waiting_for_its_result(f01, instrument, cli):
    manager = f01.manager()
    exporter = instrument(manager)
    converse(cli([image(SHOT)], gap=0.6))
    flush(manager)
    (tool,) = tool_spans(exporter)
    assert len(refs(tool)) == 1 and len(f01.uploads) == 1
    hook_ms = tool.attributes[ClaudeAgentAttributes.TOOL_DURATION_MS]
    # Ended at the PostToolUse time, not when the result streamed 600 ms later.
    assert abs(_duration_ms(tool) - hook_ms) < 150 and _duration_ms(tool) < 450
    manager.shutdown(5)


def test_a_result_that_never_arrives_still_ends_the_tool_span_at_its_hook_time(f01, instrument, cli):
    manager = f01.manager()
    exporter = instrument(manager)
    messages = converse(cli([image(SHOT)], order="no-result", gap=0.3))
    flush(manager)
    assert type(messages[-1]).__name__ == "ResultMessage"
    (tool,) = tool_spans(exporter)
    assert tool.status.is_ok and not refs(tool) and f01.uploads == []
    assert abs(_duration_ms(tool) - tool.attributes[ClaudeAgentAttributes.TOOL_DURATION_MS]) < 150
    manager.shutdown(5)


def test_url_and_file_sources_never_fetch(f01, instrument, cli):
    manager = f01.manager()
    instrument(manager)
    converse(cli([{"type": "image", "source": {"type": "url", "url": "http://127.0.0.1:9/x.png"}},
                  {"type": "image", "source": {"type": "file", "file_id": "file_1"}},
                  {"type": "image", "source": {"type": "base64", "media_type": "text/plain", "data": b64(SHOT)}}]))
    flush(manager)
    assert f01.uploads == []
    manager.shutdown(5)


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_outputs", "hide_input_images", "pii_redaction"])
def test_local_restrictions_deny(f01, instrument, cli, flag):
    manager = f01.manager()
    before = len(f01.after_bootstrap())
    instrument(manager, TraceConfig(**{flag: True}))
    converse(cli([image(SHOT)]))
    flush(manager)
    assert f01.uploads == [] and len(f01.after_bootstrap()) == before
    manager.shutdown(5)


def test_disabled_manager_uploads_nothing_and_the_conversation_is_unchanged(f01, instrument, cli):
    instrument(create_attachment_manager(config=AttachmentConfig()))
    messages = converse(cli([image(SHOT)]))
    assert f01.uploads == []
    blocks = [m for m in messages if type(m).__name__ == "UserMessage"][0].content
    assert blocks[0].content[0]["source"]["data"] == b64(SHOT)
