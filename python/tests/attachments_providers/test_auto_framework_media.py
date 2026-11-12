"""Auto with the Agno and Pydantic AI media adapters (#691): real frameworks, loopback F01.

traceai.init(auto_instrument=["agno"] / ["pydantic_ai"]) must hand each framework the ONE
shared manager in the keyword arguments it accepts, carry auto's hide flags, and stay a strict
no-op with attachments OFF. Skips when the installed framework version has no media adapter,
unless TRACEAI_EXPECT_FRAMEWORK_ADAPTERS=1, which turns the skip into a failure.
"""

import importlib
import json
import os

import pytest

traceai = pytest.importorskip("traceai")

from conftest import flush, png, refs  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

EXPECT = os.environ.get("TRACEAI_EXPECT_FRAMEWORK_ADAPTERS") == "1"
IMAGE = png(seed=81)
PROMPT = "describe this synthetic picture"


def _adapter(path, name):
    try:
        return list(getattr(importlib.import_module(path), name)["operations"])
    except Exception:
        if EXPECT:
            raise
        pytest.skip("%s has no media adapter in this version" % path)


@pytest.fixture
def auto_state(monkeypatch):
    for name, value in (("_tracer_provider", None), ("_init_options", None), ("_instrumented", set()),
                        ("_adapter_status", {}), ("_attachment_manager_borrowed", False)):
        monkeypatch.setattr(traceai, name, value)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    for key in ("OPENINFERENCE_HIDE_INPUTS", "OPENINFERENCE_HIDE_INPUT_IMAGES", "OPENINFERENCE_HIDE_OUTPUTS"):
        monkeypatch.delenv(key, raising=False)
    yield
    try:
        from traceai_agno import AgnoInstrumentorWrapper

        AgnoInstrumentorWrapper().uninstrument()
    except ImportError:
        pass
    try:
        from traceai_pydantic_ai import PydanticAIInstrumentor

        instrumentor = PydanticAIInstrumentor()
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    except ImportError:
        pass


def _init(f01, monkeypatch, libraries, enabled=True, **options):
    monkeypatch.setenv("SSL_CERT_FILE", str(f01.cert))
    monkeypatch.setenv("FI_API_KEY", "SYNTHETIC_KEY")
    monkeypatch.setenv("FI_SECRET_KEY", "SYNTHETIC_SECRET")
    if enabled:
        options["attachment_config"] = AttachmentConfig(enabled=True, endpoint=f01.endpoint)
    tracer_provider = traceai.init(project_name="synthetic", project_type="observe", auto_instrument=libraries,
                                   verbose=False, set_global_tracer_provider=False, **options)
    exporter = InMemorySpanExporter()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracer_provider, exporter


def _exported(exporter):
    return json.dumps([dict(span.attributes or {}) for span in exporter.get_finished_spans()], default=str)


# ------------------------------------------------------------------------------------- Agno
def _agno_run(provider):
    pytest.importorskip("agno")
    pytest.importorskip("openinference.instrumentation.agno")
    from agno.agent import Agent
    from agno.media import Image
    from agno.models.openai import OpenAIChat

    model = OpenAIChat(id="gpt-4o-mini", api_key="sk-synthetic", base_url=provider.url + "/v1", max_retries=0)
    try:
        agent = Agent(model=model, telemetry=False)
    except TypeError:  # older Agno has no telemetry switch; AGNO_TELEMETRY=false covers it
        agent = Agent(model=model)
    agent.run(PROMPT, images=[Image(content=IMAGE, format="png")])


def test_auto_agno_uploads_the_image_on_the_model_span(f01, provider, auto_state, monkeypatch):
    pytest.importorskip("agno")
    _adapter("traceai_agno._attachments", "agno_attachment_capabilities")
    tracer_provider, exporter = _init(f01, monkeypatch, ["agno"])
    manager = traceai.get_attachment_manager()
    try:
        assert manager.ready(5), manager.status()
        row = {r["name"]: r for r in traceai.report()["integrations"]}["agno"]
        assert (row["status"], row["media"]) == ("registered", "provider_adapter")
        _agno_run(provider)
        flush(manager)
        assert f01.payloads == [IMAGE]
        owners = [span.name for span in exporter.get_finished_spans() for _ in refs(span)]
        assert len(owners) == 1 and owners[0].endswith((".invoke", ".ainvoke")), owners
    finally:
        manager.shutdown(5)
        tracer_provider.shutdown()


def test_auto_agno_off_is_a_no_op(f01, provider, auto_state, monkeypatch):
    pytest.importorskip("agno")
    tracer_provider, exporter = _init(f01, monkeypatch, ["agno"], enabled=False)
    try:
        assert traceai.attachment_status()["adapters"]["agno"] == "registered"
        _agno_run(provider)
        assert f01.state["requests"] == []
        assert exporter.get_finished_spans() and all(not refs(s) for s in exporter.get_finished_spans())
        assert PROMPT in _exported(exporter)  # tracing itself is unchanged
    finally:
        tracer_provider.shutdown()


def test_auto_agno_honours_auto_hide_flags(f01, provider, auto_state, monkeypatch):
    """Auto's fi TraceConfig reaches Agno's OpenInference config: input text AND image stay out."""
    pytest.importorskip("agno")
    _adapter("traceai_agno._attachments", "agno_attachment_capabilities")
    tracer_provider, exporter = _init(f01, monkeypatch, ["agno"], trace_config=TraceConfig(hide_inputs=True))
    manager = traceai.get_attachment_manager()
    try:
        assert manager.ready(5), manager.status()
        _agno_run(provider)
        flush(manager)
        assert f01.payloads == []
        assert exporter.get_finished_spans()
        assert PROMPT not in _exported(exporter)
    finally:
        manager.shutdown(5)
        tracer_provider.shutdown()


# ------------------------------------------------------------------------------ Pydantic AI
def _pydantic_ai_run():
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent, BinaryContent
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def echo(messages, info):
        return ModelResponse(parts=[TextPart("ok")])

    Agent(FunctionModel(echo)).run_sync([PROMPT, BinaryContent(data=IMAGE, media_type="image/png")])


def _pydantic_ai_installed_by_name():
    """BaseInstrumentor requires the "pydantic-ai" distribution; pydantic-ai-slim alone fails its check."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        version("pydantic-ai")
        return True
    except PackageNotFoundError:
        return False


def test_auto_pydantic_ai_uploads_the_image_or_reports_the_dependency_gap(f01, auto_state, monkeypatch):
    pytest.importorskip("pydantic_ai")
    _adapter("traceai_pydantic_ai._attachments", "pydantic_ai_attachment_capabilities")
    tracer_provider, exporter = _init(f01, monkeypatch, ["pydantic-ai"])
    manager = traceai.get_attachment_manager()
    try:
        assert manager.ready(5), manager.status()
        row = {r["name"]: r for r in traceai.report()["integrations"]}["pydantic_ai"]
        if not _pydantic_ai_installed_by_name():
            # Accurate, not "registered": nothing was instrumented.
            assert row["status"] == "dependency_unavailable"
            _pydantic_ai_run()
            flush(manager)
            assert f01.payloads == [] and not exporter.get_finished_spans()
            return
        assert (row["status"], row["media"]) == ("registered", "provider_adapter")
        _pydantic_ai_run()
        flush(manager)
        assert f01.payloads == [IMAGE]
        assert len([s for s in exporter.get_finished_spans() if refs(s)]) == 1
    finally:
        manager.shutdown(5)
        tracer_provider.shutdown()


def test_auto_pydantic_ai_off_is_a_no_op(f01, auto_state, monkeypatch):
    pytest.importorskip("pydantic_ai")
    if not _pydantic_ai_installed_by_name():
        pytest.skip("pydantic-ai-slim only: auto cannot instrument it (dependency check)")
    tracer_provider, exporter = _init(f01, monkeypatch, ["pydantic-ai"], enabled=False)
    try:
        _pydantic_ai_run()
        assert f01.state["requests"] == []
        assert exporter.get_finished_spans() and all(not refs(s) for s in exporter.get_finished_spans())
    finally:
        tracer_provider.shutdown()
