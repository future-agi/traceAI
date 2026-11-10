"""Real offline agent runs must honor a non-global provider in either mode.

Each case gets a fresh interpreter because OpenTelemetry's global provider can
only be assigned once. No private provider reset or fake agent is involved.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("global_state", ["unset", "different"])
@pytest.mark.parametrize("method", ["run", "run_sync"])
@pytest.mark.parametrize("hidden", [False, True], ids=["visible", "hidden"])
@pytest.mark.parametrize("use_builtin", [True, False], ids=["builtin-alias", "wrapper"])
def test_explicit_provider_routing(global_state, method, hidden, use_builtin, tmp_path):
    # Do not pass application credentials, native tracing settings, or content
    # policy environment variables from the parent test process into the probe.
    env = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR",
    ) if key in os.environ}
    env["HOME"] = str(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
         global_state, method, str(int(hidden)), str(int(use_builtin))],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["agent_spans"] == 1
    assert result["model_spans"] == 1
    assert result["global_spans"] == 0
    assert result["global_unchanged"] is True
    print("ROUTING_PROBE " + completed.stdout.strip())


def _probe(global_state, method, hidden, use_builtin):
    import asyncio
    from importlib.metadata import version
    import socket

    # A model regression must fail locally rather than attempt an API call.
    def no_network(*args, **kwargs):
        raise AssertionError("The routing probe must not use the network")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network

    from fi_instrumentation import TraceConfig
    from opentelemetry import trace
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai import Agent, models
    from pydantic_ai.models.test import TestModel
    import traceai_pydantic_ai
    from traceai_pydantic_ai import instrument_pydantic_ai

    models.ALLOW_MODEL_REQUESTS = False
    prompt = "SYNTHETIC_ROUTING_INPUT"
    output = "SYNTHETIC_ROUTING_OUTPUT"

    class CaptureStarts(SpanProcessor):
        def __init__(self):
            self.attributes = []

        def on_start(self, span, parent_context=None):
            self.attributes.append(dict(span.attributes))

        def on_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    exporter = InMemorySpanExporter()
    explicit_provider = TracerProvider()
    starts = CaptureStarts()
    explicit_provider.add_span_processor(starts)
    explicit_provider.add_span_processor(SimpleSpanProcessor(exporter))
    global_exporter = InMemorySpanExporter()
    other_provider = TracerProvider()
    other_provider.add_span_processor(SimpleSpanProcessor(global_exporter))
    original_global = trace.get_tracer_provider()
    assert isinstance(original_global, trace.ProxyTracerProvider)
    if global_state == "different":
        trace.set_tracer_provider(other_provider)
    global_before = trace.get_tracer_provider()
    assert global_before is not explicit_provider

    # Positive control: a separately configured global exporter really works.
    # With no global set, creating a tracer must leave it a no-op instead.
    with trace.get_tracer("routing-control").start_as_current_span("control") as span:
        assert span.is_recording() is (global_state == "different")
    assert len(global_exporter.get_finished_spans()) == int(global_state == "different")
    global_exporter.clear()

    agent = Agent(TestModel(custom_output_text=output))

    def run():
        result = (asyncio.run(agent.run(prompt)) if method == "run"
                  else agent.run_sync(prompt))
        assert result.output == output

    # Existing instances must be traced, and teardown must stop exporting.
    run()
    assert not exporter.get_finished_spans()
    assert not global_exporter.get_finished_spans()
    instrumentor = instrument_pydantic_ai(
        tracer_provider=explicit_provider, use_builtin=use_builtin,
        config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden),
    )
    try:
        assert instrumentor.is_instrumented
        assert trace.get_tracer_provider() is global_before
        run()
        explicit_provider.force_flush()
        other_provider.force_flush()
        spans = exporter.get_finished_spans()
        runs = [span for span in spans if span.name == "pydantic_ai.agent.run"]
        requests = [span for span in spans if span.name == "pydantic_ai.model.request"]
        assert len(runs) == 1, [span.name for span in spans]
        assert len(requests) == 1, [span.name for span in spans]
        assert len(spans) == 2, [span.name for span in spans]
        assert requests[0].parent.span_id == runs[0].context.span_id
        assert requests[0].context.trace_id == runs[0].context.trace_id
        assert all(span.status.status_code.name == "OK" for span in spans)
        assert not global_exporter.get_finished_spans()
        assert trace.get_tracer_provider() is global_before
        captured = repr(starts.attributes) + "\n".join(span.to_json() for span in spans)
        if hidden:
            assert prompt not in captured
            assert output not in captured
        else:
            assert prompt in captured
            assert output in captured

        instrumentor.uninstrument()
        exporter.clear()
        run()
        assert not exporter.get_finished_spans()
        assert not global_exporter.get_finished_spans()
        assert trace.get_tracer_provider() is global_before
        print(json.dumps({
            "global_state": global_state, "method": method,
            "hidden": hidden, "use_builtin": use_builtin,
            "agent_spans": len(runs), "model_spans": len(requests),
            "global_spans": len(global_exporter.get_finished_spans()),
            "global_unchanged": True, "privacy_checked": True,
            "uninstrumented_and_teardown_controls": True,
            "instrumentation_file": traceai_pydantic_ai.__file__,
            "versions": {name: version(name) for name in (
                "pydantic-ai", "pydantic-ai-slim", "pydantic",
                "opentelemetry-sdk", "fi-instrumentation-otel",
            )},
        }))
    finally:
        if instrumentor.is_instrumented:
            instrumentor.uninstrument()
        explicit_provider.shutdown()
        other_provider.shutdown()


if __name__ == "__main__":
    # Resolve this checkout, not an editable install from a different tree.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    _probe(sys.argv[1], sys.argv[2], bool(int(sys.argv[3])), bool(int(sys.argv[4])))
