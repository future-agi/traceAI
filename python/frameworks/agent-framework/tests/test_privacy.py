"""Privacy: FI_HIDE_* (TraceConfig) and raw error text."""

import json

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from fi_instrumentation import TraceConfig
from traceai_agent_framework.processor import AgentFrameworkSpanProcessor

MARK = "UNIQUE_MARKER_5521"
MSGS = json.dumps([{"role": "user", "parts": [{"type": "text", "content": f"hi {MARK}"}]}])
OUT = json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": f"yo {MARK}"}]}])


def _run(config, make):
    exp = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(AgentFrameworkSpanProcessor(config))
    prov.add_span_processor(SimpleSpanProcessor(exp))
    tracer = prov.get_tracer("agent_framework")
    make(tracer)
    return exp.get_finished_spans()


def _chat(tracer):
    with tracer.start_as_current_span("chat m") as sp:
        sp.set_attribute("gen_ai.operation.name", "chat")
        sp.set_attribute("gen_ai.input.messages", MSGS)
        sp.set_attribute("gen_ai.output.messages", OUT)
        sp.set_attribute("gen_ai.system_instructions", f"sys {MARK}")


def _tool(tracer):
    with tracer.start_as_current_span("execute_tool t") as sp:
        sp.set_attribute("gen_ai.operation.name", "execute_tool")
        sp.set_attribute("gen_ai.tool.call.arguments", f'{{"q": "{MARK}"}}')
        sp.set_attribute("gen_ai.tool.call.result", f"res {MARK}")


def _dump(spans):
    return json.dumps([dict(s.attributes) for s in spans], default=str)


def test_default_keeps_content():
    assert MARK in _dump(_run(TraceConfig(), _chat))
    assert MARK in _dump(_run(TraceConfig(), _tool))


def test_hide_inputs_and_outputs_remove_marker_everywhere():
    cfg = TraceConfig(hide_inputs=True, hide_outputs=True)
    assert MARK not in _dump(_run(cfg, _chat))
    assert MARK not in _dump(_run(cfg, _tool))


def test_hide_inputs_only_keeps_output():
    spans = _run(TraceConfig(hide_inputs=True), _chat)
    attrs = dict(spans[0].attributes)
    assert not any(MARK in str(v) and "input" in k for k, v in attrs.items())
    assert any(MARK in str(v) for k, v in attrs.items() if "output" in k)
    assert "gen_ai.system_instructions" not in attrs


def test_error_text_not_exported():
    def make(tracer):
        with tracer.start_as_current_span("execute_tool t") as sp:
            sp.set_attribute("gen_ai.operation.name", "execute_tool")
            sp.set_attribute("error.type", "RuntimeError")
            try:
                raise RuntimeError(f"boom /Users/secretuser/x {MARK}")
            except RuntimeError as e:
                sp.record_exception(e)
                sp.set_status(Status(StatusCode.ERROR, repr(e)))

    spans = _run(TraceConfig(), make)
    sp = spans[0]
    assert sp.status.status_code == StatusCode.ERROR
    assert sp.status.description == "RuntimeError"
    blob = json.dumps([(e.name, dict(e.attributes)) for e in sp.events], default=str)
    assert MARK not in blob and "secretuser" not in blob and "stacktrace" not in blob
