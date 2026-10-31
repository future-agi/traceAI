"""The optional Protect hook follows the public instrumentor's lifecycle.

Use the real LangChain callback path, guardrail wrapper and OTLP/HTTP exporter.
Only the optional evaluation service is synthetic; no credentials or model calls
are required. These contracts also run when ai-evaluation is not installed.
"""

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import ModuleType

import pytest
from fi_instrumentation import TraceConfig
from langchain_core.callbacks import BaseCallbackManager
from langchain_core.runnables import RunnableLambda
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from traceai_langchain import LangChainInstrumentor


@pytest.fixture
def protect_module(monkeypatch):
    module = ModuleType("fi.evals")

    class Protect:
        def __init__(self):
            self.calls = []
            self.result = {"status": "passed", "messages": "private-output"}
            self.error = None

        def protect(self, inputs, protect_rules):
            self.calls.append((inputs, protect_rules))
            if self.error is not None:
                raise self.error
            return self.result

    module.Protect = Protect
    package = ModuleType("fi")
    package.__path__ = []
    package.evals = module
    monkeypatch.setitem(sys.modules, "fi", package)
    monkeypatch.setitem(sys.modules, "fi.evals", module)
    return module


@pytest.fixture
def instrumentor():
    instance = LangChainInstrumentor()
    original_init = BaseCallbackManager.__init__
    try:
        yield instance
    finally:
        if instance.is_instrumented_by_opentelemetry:
            instance.uninstrument()
        # Keep baseline failures from contaminating other tests.
        BaseCallbackManager.__init__ = original_init


@pytest.fixture
def otlp():
    payloads = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
                headers={},
                timeout=2,
            )
        )
    )

    def spans():
        return [
            span
            for payload in payloads
            for resource in ExportTraceServiceRequest.FromString(payload).resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]

    try:
        yield provider, spans
    finally:
        provider.shutdown()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_uninstrument_restores_both_methods(protect_module, instrumentor, otlp):
    provider, _ = otlp
    original_init = BaseCallbackManager.__init__
    original_protect = protect_module.Protect.protect
    instrumentor.instrument(tracer_provider=provider)
    assert BaseCallbackManager.__init__ is not original_init
    assert protect_module.Protect.protect is not original_protect

    instrumentor.uninstrument()

    assert BaseCallbackManager.__init__ is original_init
    assert protect_module.Protect.protect is original_protect
    assert instrumentor._original_protect is None


def test_disable_stops_guardrail_exports(protect_module, instrumentor, otlp):
    provider, spans = otlp
    client = protect_module.Protect()
    rules = [{"metric": "synthetic"}]
    instrumentor.instrument(tracer_provider=provider)
    assert client.protect("enabled", rules) is client.result
    assert [span.name for span in spans()] == ["Guardrail.protect"]

    instrumentor.uninstrument()

    assert client.protect("disabled", rules) is client.result
    assert client.calls == [("enabled", rules), ("disabled", rules)]
    assert [span.name for span in spans()] == ["Guardrail.protect"]


def test_repeated_cycles_keep_one_guardrail_span(protect_module, instrumentor, otlp):
    provider, spans = otlp
    client = protect_module.Protect()
    rules = [{"metric": "synthetic"}]
    chain = RunnableLambda(lambda value: client.protect(value, rules))

    for cycle in range(3):
        instrumentor.instrument(tracer_provider=provider)
        with provider.get_tracer(__name__).start_as_current_span("outer"):
            assert chain.invoke("enabled") is client.result
        exported = spans()
        assert len(exported) == (cycle + 1) * 3
        guardrail, chain_span, parent = exported[-3:]
        assert guardrail.name == "Guardrail.protect"
        assert parent.name == "outer"
        assert guardrail.parent_span_id == chain_span.parent_span_id == parent.span_id
        assert guardrail.trace_id == chain_span.trace_id == parent.trace_id
        assert guardrail.end_time_unix_nano >= guardrail.start_time_unix_nano > 0
        assert guardrail.status.code == 1  # OTLP STATUS_CODE_OK
        instrumentor.uninstrument()
        assert chain.invoke("disabled") is client.result
        assert len(spans()) == (cycle + 1) * 3

    assert len(client.calls) == 6


def test_reenable_drops_old_provider_and_config(protect_module, instrumentor, otlp):
    provider, spans = otlp
    client = protect_module.Protect()
    rules = [{"metric": "synthetic"}]
    instrumentor.instrument(tracer_provider=provider)
    assert client.protect("first-input", rules) is client.result
    instrumentor.uninstrument()

    # A different provider must not keep exporting to the previous one.
    new_provider = TracerProvider()
    try:
        instrumentor.instrument(tracer_provider=new_provider)
        assert client.protect("second-input", rules) is client.result
        instrumentor.uninstrument()
        assert len(spans()) == 1
    finally:
        new_provider.shutdown()

    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=True, hide_outputs=True),
    )
    assert client.protect("private-input", rules) is client.result
    assert len(spans()) == 2
    hidden = str(spans()[-1])
    assert "private-input" not in hidden
    assert "private-output" not in hidden
    instrumentor.uninstrument()


def test_missing_optional_dependency_keeps_callback_lifecycle(monkeypatch, instrumentor, otlp):
    monkeypatch.setitem(sys.modules, "fi.evals", None)
    provider, spans = otlp
    original_init = BaseCallbackManager.__init__
    instrumentor.instrument(tracer_provider=provider)
    assert RunnableLambda(lambda value: value + 1).invoke(1) == 2
    instrumentor.uninstrument()
    assert BaseCallbackManager.__init__ is original_init
    assert len(spans()) == 1
    assert RunnableLambda(lambda value: value + 1).invoke(2) == 3
    assert len(spans()) == 1


def test_optional_dependency_changes_do_not_leave_stale_hooks(
    monkeypatch, protect_module, instrumentor, otlp
):
    provider, _ = otlp
    original_protect = protect_module.Protect.protect
    instrumentor.instrument(tracer_provider=provider)
    # Cleanup must restore the captured class, not require another import.
    monkeypatch.setitem(sys.modules, "fi.evals", None)
    instrumentor.uninstrument()
    assert protect_module.Protect.protect is original_protect

    instrumentor.instrument(tracer_provider=provider)
    instrumentor.uninstrument()
    assert protect_module.Protect.protect is original_protect

    monkeypatch.setitem(sys.modules, "fi.evals", protect_module)
    instrumentor.instrument(tracer_provider=provider)
    assert protect_module.Protect.protect is not original_protect
    instrumentor.uninstrument()
    assert protect_module.Protect.protect is original_protect


def test_public_lifecycle_is_idempotent(protect_module, instrumentor, otlp):
    provider, spans = otlp
    client = protect_module.Protect()
    original_protect = protect_module.Protect.protect
    instrumentor.instrument(tracer_provider=provider)
    instrumentor.instrument(tracer_provider=provider)
    assert client.protect("input", [{"metric": "synthetic"}]) is client.result
    assert len(spans()) == 1
    instrumentor.uninstrument()
    instrumentor.uninstrument()
    assert protect_module.Protect.protect is original_protect


def test_exceptions_are_unchanged_after_disable(protect_module, instrumentor, otlp):
    provider, spans = otlp
    client = protect_module.Protect()
    client.error = ValueError("synthetic guardrail failure")
    instrumentor.instrument(tracer_provider=provider)
    for enabled in (True, False):
        if not enabled:
            instrumentor.uninstrument()
        with pytest.raises(ValueError) as caught:
            client.protect("input", [{"metric": "synthetic"}])
        assert caught.value is client.error
        assert len(spans()) == 1
        assert spans()[0].status.code == 2  # OTLP STATUS_CODE_ERROR
