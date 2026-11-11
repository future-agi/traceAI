"""The real auto path: traceai.init(attachment_config=enabled) -> OpenAI/Anthropic capture -> loopback F01."""

import pytest

traceai = pytest.importorskip("traceai")
openai = pytest.importorskip("openai")
pytest.importorskip("traceai_openai")

from conftest import b64, flush, png, refs  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, CapturePolicy  # noqa: E402
from opentelemetry import context as otel_context  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402


@pytest.fixture
def auto_state(monkeypatch):
    monkeypatch.setattr(traceai, "_tracer_provider", None)
    monkeypatch.setattr(traceai, "_init_options", None)
    monkeypatch.setattr(traceai, "_instrumented", set())
    monkeypatch.setattr(traceai, "_adapter_status", {})
    monkeypatch.setattr(traceai, "_attachment_manager_borrowed", False)
    yield
    for module, name in (("traceai_openai", "OpenAIInstrumentor"), ("traceai_anthropic", "AnthropicInstrumentor"),
                         ("traceai_groq", "GroqInstrumentor")):
        try:
            instrumentor = getattr(__import__(module), name)()
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
        except ImportError:
            pass


def test_auto_init_with_enabled_attachments_captures_chat_image(f01, provider, auto_state, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", str(f01.cert))
    monkeypatch.setenv("FI_API_KEY", "SYNTHETIC_KEY")
    monkeypatch.setenv("FI_SECRET_KEY", "SYNTHETIC_SECRET")
    tracer_provider = traceai.init(project_name="synthetic", project_type="observe", auto_instrument=["openai"],
                                   verbose=False, set_global_tracer_provider=False,
                                   attachment_config=AttachmentConfig(enabled=True, endpoint=f01.endpoint))
    exporter = InMemorySpanExporter()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    manager = tracer_provider.attachment_manager
    try:
        assert manager.ready(5), manager.status()
        image = png(seed=31)
        openai.OpenAI(api_key="sk-synthetic", base_url=provider.url + "/v1", max_retries=0).chat.completions.create(
            model="m", messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(image)}}]}])
        flush(manager)
        assert f01.payloads == [image]
        assert traceai.attachment_status()["runtime"] == "ready"
        span = [s for s in exporter.get_finished_spans() if "fi.attachment.refs" in s.attributes]
        assert span, [s.name for s in exporter.get_finished_spans()]
    finally:
        manager.shutdown(5)
        tracer_provider.shutdown()


def test_auto_init_with_attachments_off_is_unchanged(provider, auto_state):
    tracer_provider = traceai.init(project_name="synthetic", auto_instrument=["openai"], verbose=False,
                                   set_global_tracer_provider=False)
    try:
        assert tracer_provider.attachment_manager.status().state == "disabled"
        from traceai_openai import OpenAIInstrumentor

        wrapper = openai.OpenAI.request._self_wrapper
        assert wrapper._attachments is None
        assert OpenAIInstrumentor().is_instrumented_by_opentelemetry
    finally:
        tracer_provider.shutdown()


def _enabled_init(f01, monkeypatch, libraries, **options):
    monkeypatch.setenv("SSL_CERT_FILE", str(f01.cert))
    monkeypatch.setenv("FI_API_KEY", "SYNTHETIC_KEY")
    monkeypatch.setenv("FI_SECRET_KEY", "SYNTHETIC_SECRET")
    if "attachment_manager" not in options:
        options["attachment_config"] = AttachmentConfig(enabled=True, endpoint=f01.endpoint)
    tracer_provider = traceai.init(project_name="synthetic", project_type="observe", auto_instrument=libraries,
                                   verbose=False, set_global_tracer_provider=False, **options)
    exporter = InMemorySpanExporter()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracer_provider, exporter


def _chat_image(provider, image):
    openai.OpenAI(api_key="sk-synthetic", base_url=provider.url + "/v1", max_retries=0).chat.completions.create(
        model="m", messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(image)}}]}])


def _shot(data):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64(data)}}


def test_auto_init_captures_openai_image_and_anthropic_computer_screenshot(f01, provider, auto_state, monkeypatch):
    """A01 end to end: ONE manager from traceai.init reaches both instrumentors and the loopback F01."""
    anthropic = pytest.importorskip("anthropic")
    pytest.importorskip("traceai_anthropic")
    tracer_provider, exporter = _enabled_init(f01, monkeypatch, ["openai", "anthropic"])
    manager = traceai.get_attachment_manager()
    try:
        assert manager is tracer_provider.attachment_manager
        assert manager.ready(5), manager.status()
        image, screenshot = png(seed=51), png(seed=52)
        _chat_image(provider, image)
        anthropic.Anthropic(api_key="sk-synthetic", base_url=provider.url, max_retries=0).messages.create(
            model="claude-test", max_tokens=16,
            tools=[{"type": "computer_20250124", "name": "computer", "display_width_px": 1024,
                    "display_height_px": 768}],
            messages=[{"role": "user", "content": [{"type": "text", "text": "open the page"}]},
                      {"role": "assistant", "content": [
                          {"type": "tool_use", "id": "toolu_9", "name": "computer", "input": {"action": "screenshot"}}]},
                      {"role": "user", "content": [
                          {"type": "tool_result", "tool_use_id": "toolu_9", "content": [_shot(screenshot)]}]}])
        flush(manager)
        assert sorted(f01.payloads) == sorted([image, screenshot])
        uploads = {u["purpose"]: u for u in f01.uploads}
        assert uploads["computer_screenshot"].get("tool_call_id") == "toolu_9"
        assert uploads["model_input"]["direction"] == "input"
        span_refs = [ref for span in exporter.get_finished_spans() for ref in refs(span)]
        assert sorted(ref["purpose"] for ref in span_refs) == ["computer_screenshot", "model_input"]
        shot = [ref for ref in span_refs if ref["purpose"] == "computer_screenshot"][0]
        assert (shot["message_index"], shot["part_index"], shot["role"]) == (2, 0, "tool")
        exported = str([dict(span.attributes) for span in exporter.get_finished_spans()])
        assert b64(image) not in exported and b64(screenshot) not in exported
        report = traceai.report()
        assert report["attachments"]["state"] == "ready"
        rows = {row["name"]: row for row in report["integrations"]}
        for name in ("openai", "anthropic"):
            assert (rows[name]["status"], rows[name]["media"]) == ("registered", "provider_adapter")
        assert "tool_result.input" in rows["anthropic"]["media_operations"]
        assert "SYNTHETIC" not in str(report) and str(f01.server.server_port) not in str(report)
    finally:
        manager.shutdown(5)
        tracer_provider.shutdown()


def test_report_rows_for_installed_media_adapters(auto_state):
    pytest.importorskip("traceai_groq")
    rows = {row["name"]: row for row in traceai.report()["integrations"]}
    assert {name: rows[name]["media"] for name in ("openai", "anthropic", "groq")} == dict.fromkeys(
        ("openai", "anthropic", "groq"), "provider_adapter")
    assert rows["litellm"]["media"] == "unqualified"


def test_auto_borrowed_manager_is_shared_and_tightened(f01, provider, auto_state, monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

    borrowed = f01.manager()
    sdk = SdkTracerProvider()
    try:
        with sdk.get_tracer("synthetic").start_as_current_span("probe") as span:
            ticket = borrowed.preflight(span, mime_type="image/png", direction="input", max_bytes=16)
            assert ticket.admitted, ticket.reason
            ticket.release()
        tracer_provider, _ = _enabled_init(f01, monkeypatch, ["openai"], attachment_manager=borrowed,
                                           trace_config=TraceConfig(pii_redaction=True))
        assert traceai.get_attachment_manager() is borrowed is tracer_provider.attachment_manager
        assert openai.OpenAI.request._self_wrapper._attachments is borrowed
        # Auto's PII flag now restricts the application's manager itself, for every caller.
        with sdk.get_tracer("synthetic").start_as_current_span("probe") as span:
            assert borrowed.preflight(span, mime_type="image/png", direction="input",
                                      max_bytes=16).reason == "policy_denied"
        _chat_image(provider, png(seed=53))
        flush(borrowed)
        assert f01.payloads == []
        tracer_provider.shutdown()
        # Auto never shuts a borrowed manager down.
        assert borrowed.status().state == "ready"
    finally:
        borrowed.shutdown(5)
        sdk.shutdown()


def test_auto_kill_switch_after_init_denies_every_admission(f01, provider, auto_state, monkeypatch):
    tracer_provider, _ = _enabled_init(f01, monkeypatch, ["openai"])
    manager = traceai.get_attachment_manager()
    try:
        assert manager.ready(5)
        monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
        _chat_image(provider, png(seed=54))
        flush(manager)
        assert f01.payloads == []
        assert traceai.report()["attachments"]["state"] == "disabled"
        monkeypatch.delenv("FI_ATTACHMENTS_ENABLED")
        image = png(seed=55)
        _chat_image(provider, image)
        flush(manager)
        assert f01.payloads == [image]
    finally:
        manager.shutdown(5)
        tracer_provider.shutdown()


def test_auto_kill_switch_at_init_keeps_the_old_code_path(f01, provider, auto_state, monkeypatch):
    monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
    tracer_provider, _ = _enabled_init(f01, monkeypatch, ["openai"])
    try:
        assert tracer_provider.attachment_manager.status().state == "disabled"
        assert openai.OpenAI.request._self_wrapper._attachments is None
        _chat_image(provider, png(seed=56))
        assert f01.state["requests"] == []  # not even a session bootstrap
    finally:
        tracer_provider.shutdown()


def test_auto_per_call_policy_and_ownership(f01, provider, auto_state, monkeypatch):
    tracer_provider, exporter = _enabled_init(f01, monkeypatch, ["openai"])
    manager = traceai.get_attachment_manager()
    try:
        assert manager.ready(5)
        for context in (traceai.with_attachment_capture_policy(CapturePolicy(capture_content=False)),
                        traceai.with_attachment_capture_policy(CapturePolicy(muted=True)),
                        traceai.with_attachment_ownership()):
            token = otel_context.attach(context)
            try:
                _chat_image(provider, png(seed=57))
            finally:
                otel_context.detach(token)
        flush(manager)
        assert f01.payloads == []
        assert not any(refs(span) for span in exporter.get_finished_spans())
        image = png(seed=58)
        token = otel_context.attach(traceai.with_attachment_capture_policy(CapturePolicy()))
        try:
            _chat_image(provider, image)
        finally:
            otel_context.detach(token)
        flush(manager)
        assert f01.payloads == [image]
    finally:
        manager.shutdown(5)
        tracer_provider.shutdown()
