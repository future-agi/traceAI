"""Real Anthropic SDK -> traceai_anthropic -> loopback F01: vision, PDF and computer-use screenshots."""

import asyncio
import json

import pytest

anthropic = pytest.importorskip("anthropic")

from conftest import PDF, b64, flush, png, refs  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from traceai_anthropic import AnthropicInstrumentor  # noqa: E402

IMAGE = png(seed=11)
SHOT_1 = png(seed=12)
SHOT_2 = png(seed=13)
TOOL_IMAGE = png(seed=14)


@pytest.fixture
def instrument(spans):
    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None):
        AnthropicInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                           config=config or TraceConfig())
        state["on"] = True
        return exporter

    yield install
    if state.get("on"):
        AnthropicInstrumentor().uninstrument()


def image(data, media_type="image/png"):
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64(data)}}


def computer_use_request(stream=False):
    """A computer-use loop: two screenshots returned for the computer tool, one ordinary tool image."""
    return dict(
        model="claude-test", max_tokens=16, stream=stream,
        tools=[{"type": "computer_20250124", "name": "computer", "display_width_px": 1024,
                "display_height_px": 768},
               {"name": "chart", "description": "draws", "input_schema": {"type": "object"}}],
        messages=[
            {"role": "user", "content": [
                {"type": "text", "text": "open the page"}, image(IMAGE),
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64(PDF)}},
                {"type": "image", "source": {"type": "url", "url": "https://example.invalid/x.png"}},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "computer", "input": {"action": "screenshot"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [image(SHOT_1)]}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_2", "name": "computer", "input": {"action": "left_click"}},
                {"type": "tool_use", "id": "toolu_3", "name": "chart", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": [image(SHOT_2)]},
                {"type": "tool_result", "tool_use_id": "toolu_3", "content": [
                    {"type": "text", "text": "chart"}, image(TOOL_IMAGE)]}]},
        ])


def run(provider, request, async_):
    if async_:
        async def go():
            result = await anthropic.AsyncAnthropic(api_key="sk-synthetic", base_url=provider.url,
                                                    max_retries=0).messages.create(**request)
            if request.get("stream"):
                async for _ in result:
                    pass
        asyncio.run(go())
    else:
        result = anthropic.Anthropic(api_key="sk-synthetic", base_url=provider.url,
                                     max_retries=0).messages.create(**request)
        if request.get("stream"):
            for _ in result:
                pass


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_vision_document_and_computer_screenshots(f01, provider, instrument, async_, stream):
    manager = f01.manager()
    exporter = instrument(manager)
    run(provider, computer_use_request(stream), async_)
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, PDF, SHOT_1, SHOT_2, TOOL_IMAGE])
    span = [s for s in exporter.get_finished_spans() if s.name in ("Messages", "AsyncMessages")][-1]
    span_refs = refs(span)
    by_purpose = {}
    for ref in span_refs:
        by_purpose.setdefault(ref["purpose"], []).append(ref)
    # Causal order for the filmstrip: screenshots keep their message/block positions.
    shots = sorted(by_purpose["computer_screenshot"], key=lambda r: r["message_index"])
    assert [(r["message_index"], r["part_index"], r["role"]) for r in shots] == [(2, 0, "tool"), (4, 0, "tool")]
    assert [(r["message_index"], r["part_index"]) for r in by_purpose["tool_result"]] == [(4, 1)]
    assert [(r["message_index"], r["part_index"]) for r in by_purpose["model_input"]] == [(0, 1)]
    assert by_purpose["document"][0]["mime_type"] == "application/pdf"
    tool_ids = sorted(u.get("tool_call_id") for u in f01.uploads if u.get("tool_call_id"))
    assert tool_ids == ["toolu_1", "toolu_2", "toolu_3"]
    exported = json.dumps(dict(span.attributes))
    for data in (IMAGE, PDF, SHOT_1, SHOT_2, TOOL_IMAGE):
        assert b64(data) not in exported
    # The existing attribute schema keeps one content value per message (a later tool_result in the same
    # message overwrites an earlier one), so check the URIs on positions it always exports.
    exported_positions = {(0, 1), (2, 0)}
    for ref in span_refs:
        if (ref["message_index"], ref["part_index"]) in exported_positions:
            assert "fi-attachment:" + ref["occurrence_id"] in exported
    # The application's request reached the provider unchanged.
    sent = provider.requests[-1][1]
    assert sent["messages"][2]["content"][0]["content"][0]["source"]["data"] == b64(SHOT_1)
    manager.shutdown(5)


def test_screenshot_needs_typed_computer_tool(f01, provider, instrument):
    manager = f01.manager()
    instrument(manager)
    request = computer_use_request()
    request["tools"] = request["tools"][1:]  # no computer tool declared
    run(provider, request, False)
    flush(manager)
    purposes = sorted(u["purpose"] for u in f01.uploads)
    assert "computer_screenshot" not in purposes and purposes.count("tool_result") == 3
    manager.shutdown(5)


def test_invalid_tool_use_id_fails_closed(f01, provider, instrument):
    manager = f01.manager()
    instrument(manager)
    run(provider, dict(model="m", max_tokens=8, messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "bad id!", "content": [image(SHOT_1)]}]}]), False)
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


def test_error_tool_result_respects_hide_error_details(f01, provider, instrument):
    manager = f01.manager()
    instrument(manager, config=TraceConfig(hide_error_details=True))
    run(provider, dict(model="m", max_tokens=8, messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_9", "is_error": True, "content": [image(SHOT_1)]},
        image(IMAGE)]}]), False)
    flush(manager)
    assert f01.payloads == [IMAGE]
    manager.shutdown(5)


@pytest.mark.parametrize("config", [
    TraceConfig(hide_input_images=True), TraceConfig(hide_inputs=True), TraceConfig(pii_redaction=True),
    TraceConfig(hide_input_messages=True),
])
def test_local_restrictions_deny_screenshots(f01, provider, instrument, config):
    manager = f01.manager(allow_unredacted_media_with_text_pii=True)
    instrument(manager, config=config)
    run(provider, computer_use_request(), False)
    flush(manager)
    for shot in (SHOT_1, SHOT_2, TOOL_IMAGE, IMAGE):
        assert shot not in f01.payloads  # no model-bound screenshot escapes as tool output
    manager.shutdown(5)


def test_disabled_manager_keeps_tracing_unchanged(provider, instrument):
    manager = create_attachment_manager(config=AttachmentConfig(enabled=False))
    exporter = instrument(manager)
    run(provider, computer_use_request(), False)
    span = [s for s in exporter.get_finished_spans() if s.name == "Messages"][-1]
    assert "fi.attachment.refs" not in span.attributes
    assert "fi-attachment:" not in json.dumps(dict(span.attributes))


def test_second_observation_returns_first_occurrence(f01, spans):
    from traceai_anthropic import capture_anthropic_attachments, project_anthropic_media

    tracer_provider, _ = spans
    manager = f01.manager()
    request = computer_use_request()
    with tracer_provider.get_tracer("t").start_as_current_span("llm") as span:
        first = capture_anthropic_attachments(manager, span, request)
        second = capture_anthropic_attachments(manager, span, request)
    flush(manager)
    assert first == second and len(first) == 5 and len(f01.payloads) == 5
    assert project_anthropic_media(request, {}) is request
    manager.shutdown(5)
