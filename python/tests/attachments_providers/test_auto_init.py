"""The real auto path: traceai.init(attachment_config=enabled) -> OpenAI/Anthropic capture -> loopback F01."""

import pytest

traceai = pytest.importorskip("traceai")
openai = pytest.importorskip("openai")

from conftest import b64, flush, png  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402


@pytest.fixture
def auto_state(monkeypatch):
    monkeypatch.setattr(traceai, "_tracer_provider", None)
    monkeypatch.setattr(traceai, "_init_options", None)
    monkeypatch.setattr(traceai, "_instrumented", set())
    monkeypatch.setattr(traceai, "_adapter_status", {})
    yield
    for module, name in (("traceai_openai", "OpenAIInstrumentor"), ("traceai_anthropic", "AnthropicInstrumentor")):
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
