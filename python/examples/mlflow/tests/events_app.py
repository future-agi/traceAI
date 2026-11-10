"""Exercise the recipe's event filters through MLflow's real OTLP exporter."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mlflow
from mlflow.entities import SpanEvent

import app

app.check_environment()
mode = sys.argv[1]
processors = [app.drop_exception_events]
if mode == "content":
    processors.append(app.drop_content)
mlflow.tracing.configure(span_processors=processors)

with mlflow.start_span(name="event_privacy") as span:
    span.add_event(SpanEvent(name="checkpoint", timestamp=123456789, attributes={"attempt": 2}))
    span.add_event(SpanEvent(name="exception", timestamp=123456790,
                             attributes={"exception.message": "PRIVATE-EXCEPTION"}))
    span.add_event(SpanEvent(name="gen_ai.user.message", timestamp=123456791,
                             attributes={"content": "PRIVATE-PROMPT"}))
    span.add_event(SpanEvent(name="custom", timestamp=123456792,
                             attributes={"output": "PRIVATE-RESULT"}))
    if mode == "unsupported":
        underlying = span._span

        class UnsupportedSpan:
            @property
            def events(self):
                return underlying.events

            def __getattr__(self, name):
                if name == "_events":
                    raise AttributeError(name)
                return getattr(underlying, name)

        span._span = UnsupportedSpan()
