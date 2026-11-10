"""Privacy regression tests with MLflow 3.16.1 and real OpenTelemetry spans.

Run with unittest discovery after installing the pinned requirements. The
snippet loader executes only the privacy functions, never the sample app.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mlflow.entities import LiveSpan, SpanStatusCode
from mlflow.tracing.utils.processor import apply_span_processors
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


SOURCE = Path(__file__).resolve().parents[1] / "src/app.py"


def processors():
    text = SOURCE.read_text()
    if SOURCE.suffix == ".mdx":
        text = text.split("def _filter_events(", 1)[1]
        text = "def _filter_events(" + text.split("mlflow.tracing.configure(span_processors=", 1)[0]
    tree = ast.parse(text)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_filter_events", "drop_exception_events", "drop_content"}]
    assert len(functions) == 3
    namespace = {"SpanStatusCode": SpanStatusCode}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class EventPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.functions = processors()
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.otel = self.provider.get_tracer("privacy-regression").start_span("test")
        self.span = LiveSpan(self.otel, trace_id="tr-privacy-test")
        self.span.set_inputs({"prompt": "PRIVATE-INPUT"})
        self.span.set_outputs({"answer": "PRIVATE-OUTPUT"})
        self.span.set_status("ERROR")

    def tearDown(self):
        self.provider.shutdown()

    def add_events(self):
        self.otel.add_event("checkpoint", {"attempt": 2}, timestamp=123456789)
        self.otel.add_event("exception", {"exception.message": "PRIVATE-ERROR"})
        self.otel.add_event("provider_failure", {"exception.stacktrace": "PRIVATE-STACK"})
        self.otel.add_event("gen_ai.user.message", {"role": "user", "content": "PRIVATE-PROMPT"})
        self.otel.add_event("custom", {"output": "PRIVATE-RESULT"})
        self.otel.add_event("gen_ai.client.operation", {"gen_ai.input.messages": "PRIVATE-MESSAGE"})
        self.otel.add_event("gen_ai.client.operation.details", {"gen_ai.usage.input_tokens": 10})

    def exported(self):
        self.otel.end()
        (span,) = self.exporter.get_finished_spans()
        return span

    def test_exception_filter_preserves_other_events_and_timestamp(self):
        self.add_events()
        original = self.otel.events[0]
        self.functions["drop_exception_events"](self.span)
        # Later processors may still use the supported add_event API.
        self.otel.add_event("after_filter", {"attempt": 3})
        span = self.exported()
        self.assertEqual([event.name for event in span.events],
                         ["checkpoint", "gen_ai.user.message", "custom", "gen_ai.client.operation",
                          "gen_ai.client.operation.details", "after_filter"])
        self.assertIs(span.events[0], original)
        self.assertEqual(span.events[0].timestamp, 123456789)
        self.assertEqual(span.events[0].attributes["attempt"], 2)
        self.assertEqual(span.status.status_code.name, "ERROR")

    def test_content_filter_removes_only_sensitive_events(self):
        self.add_events()
        original = (self.otel.events[0], self.otel.events[-1])
        self.functions["drop_content"](self.span)
        # Repeated filtering must preserve the safe event exactly once.
        self.functions["drop_content"](self.span)
        self.assertEqual(self.span.inputs, "[REDACTED]")
        self.assertEqual(self.span.outputs, "[REDACTED]")
        span = self.exported()
        self.assertEqual(tuple(span.events), original)
        self.assertNotIn("PRIVATE-", span.to_json())
        self.assertEqual(span.status.status_code.name, "ERROR")
        self.assertFalse(span.status.description)

    def test_empty_and_safe_events_need_no_backing_collection_write(self):
        self.functions["drop_exception_events"](self.span)
        self.otel.add_event("checkpoint", {"attempt": 1})
        before = self.otel._events
        self.functions["drop_exception_events"](self.span)
        self.assertIs(self.otel._events, before)
        self.assertEqual(len(self.exported().events), 1)

    def test_missing_private_events_fails_closed_through_mlflow_hook(self):
        class UnsupportedSpan:
            events = (SimpleNamespace(name="exception", attributes={"exception.message": "PRIVATE"}),)

        self.span._span = UnsupportedSpan()
        config = SimpleNamespace(span_processors=[self.functions["drop_exception_events"]])
        with patch("mlflow.tracing.config.get_config", return_value=config):
            # An ordinary Exception would be swallowed by this real MLflow hook.
            with self.assertRaisesRegex(SystemExit, "refusing to export"):
                apply_span_processors(self.span)
                self.otel.end()  # must not be reached
        self.assertEqual(self.exporter.get_finished_spans(), ())

    def test_unreadable_events_fail_closed_without_sensitive_diagnostic(self):
        class UnsupportedSpan:
            @property
            def events(self):
                raise RuntimeError("PRIVATE-EVENT-CONTENT")

        with self.assertRaises(SystemExit) as caught:
            self.functions["drop_exception_events"](SimpleNamespace(_span=UnsupportedSpan()))
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_ignored_write_fails_closed(self):
        class ReadOnlySpan:
            events = (SimpleNamespace(name="exception", attributes={}),)
            _events = events

        with self.assertRaisesRegex(SystemExit, "refusing to export"):
            self.functions["drop_exception_events"](SimpleNamespace(_span=ReadOnlySpan()))


if __name__ == "__main__":
    unittest.main()
