"""Public API regression; also runnable against the pre-repair package."""

from google.adk.agents import BaseAgent
from google.adk.runners import Runner
from opentelemetry.sdk.trace import TracerProvider

from traceai_google_adk import GoogleADKInstrumentor


def test_instrument_and_uninstrument_with_current_adk():
    instrumentor = GoogleADKInstrumentor()
    provider = TracerProvider()
    originals = Runner.run_async, BaseAgent.run_async
    try:
        instrumentor.instrument(tracer_provider=provider)
        assert instrumentor.is_instrumented_by_opentelemetry
        instrumentor.uninstrument()
        assert not instrumentor.is_instrumented_by_opentelemetry
        assert (Runner.run_async, BaseAgent.run_async) == originals
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        provider.shutdown()
