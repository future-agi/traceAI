import json
import unittest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from traceai_cognee import configure, __version__

class TestAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = TracerProvider()
        trace.set_tracer_provider(cls.provider)

    def setUp(self):
        self.other = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.other))
        self.sink = InMemorySpanExporter()

    def emit(self, name, attrs):
        with self.provider.get_tracer('test').start_as_current_span(name) as span:
            for key, value in attrs.items(): span.set_attribute(key, value)
            span.record_exception(ValueError('fake-private-error'))
            span.set_status(Status(StatusCode.ERROR, 'fake-private-error'))

    def test_projection_copy_and_status(self):
        handle = configure(project_name='sample', exporter=self.sink)
        self.assertIs(handle, configure(project_name='sample', exporter=self.sink))
        with self.assertRaises(ValueError): configure(project_name='different', exporter=self.sink)
        self.emit('cognee.search.query', {'cognee.search.query':'fake-private-query', 'custom.secret':'fake-secret', 'gen_ai.usage.input_tokens':7})
        self.assertTrue(handle.force_flush())
        src = self.other.get_finished_spans()[-1]; dst = self.sink.get_finished_spans()[-1]
        self.assertEqual(src.attributes['cognee.search.query'], 'fake-private-query')
        self.assertNotIn('cognee.search.query', dst.attributes)
        self.assertNotIn('custom.secret', dst.attributes)
        self.assertEqual(dst.attributes['gen_ai.usage.input_tokens'], 7)
        self.assertEqual(dst.attributes['fi.span.kind'], 'RETRIEVER')
        self.assertEqual(dst.status.status_code, StatusCode.ERROR)
        self.assertIsNone(dst.status.description)
        self.assertEqual(dst.context, src.context); self.assertEqual(dst.parent, src.parent)
        self.assertEqual(dst.start_time, src.start_time); self.assertEqual(dst.end_time, src.end_time)
        self.assertEqual(dst.resource.attributes['project_name'], 'sample')
        self.assertNotIn('fake-private-error', str(dst.events))
        handle.shutdown()
        self.emit('after', {})
        self.assertEqual(self.other.get_finished_spans()[-1].name, 'after')

    def test_content_redaction_and_llm(self):
        handle = configure(project_name='sample', exporter=self.sink, capture_content=True)
        value = json.dumps({'api_key':'fake-secret', 'question':'hello'})
        self.emit('cognee.observe.acreate_structured_output', {'langfuse.observation.input':value, 'langfuse.observation.output':'answer'})
        handle.force_flush(); out = self.sink.get_finished_spans()[-1]
        self.assertEqual(out.attributes['fi.span.kind'], 'LLM')
        self.assertEqual(json.loads(out.attributes['input.value'])['api_key'], '[REDACTED]')
        self.assertEqual(out.attributes['output.value'], 'answer')
        self.assertNotIn('fake-secret', str(out.attributes))
        handle.shutdown()

    def test_additional_processor_and_lifecycle(self):
        handle = configure(project_name='sample', exporter=self.sink)
        native_buffer = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(native_buffer))
        self.emit('memory.retrieve', {'memory.query.text': 'question'})
        self.assertTrue(handle.force_flush())
        self.assertEqual(len(self.sink.get_finished_spans()), 1)
        self.assertEqual(native_buffer.get_finished_spans()[-1].attributes['memory.query.text'], 'question')
        handle.shutdown()
        handle.shutdown()
        self.emit('later', {})
        self.assertTrue(self.provider.force_flush())
        self.assertEqual(native_buffer.get_finished_spans()[-1].name, 'later')
        second = InMemorySpanExporter()
        next_handle = configure(project_name='sample', exporter=second)
        self.emit('reconfigured', {})
        self.assertTrue(self.provider.force_flush())
        self.assertEqual([span.name for span in second.get_finished_spans()], ['reconfigured'])
        next_handle.shutdown()
        self.assertTrue(self.provider.force_flush())

    def test_replaceable_default_refused_without_replacement(self):
        self.provider._default_processor = True
        try:
            before = self.provider._active_span_processor._span_processors
            with self.assertRaisesRegex(RuntimeError, 'replaceable default'):
                configure(project_name='sample', exporter=self.sink)
            self.assertEqual(before, self.provider._active_span_processor._span_processors)
        finally:
            del self.provider._default_processor

    def test_default_exporter_constructs_without_sending(self):
        handle = configure(project_name='sample', endpoint='http://127.0.0.1:1/v1/traces', headers={'x-api-key': 'synthetic'})
        self.assertTrue(handle.force_flush())
        handle.shutdown()

    def test_version(self): self.assertEqual(__version__, '0.1.0')
