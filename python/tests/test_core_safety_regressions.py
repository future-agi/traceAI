"""Core safety regressions; runnable with unittest against source or a wheel."""
import asyncio
import signal
import unittest

from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult

from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig, suppress_tracing
from fi_instrumentation.otel import TracerProvider as FITracerProvider


class CaptureSampler(Sampler):
    def __init__(self):
        self.seen = []

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
        self.seen.append(dict(attributes or {}))
        return SamplingResult(Decision.DROP if (attributes or {}).get('drop') else Decision.RECORD_AND_SAMPLE)

    def get_description(self):
        return 'capture'


class CoreSafetyTests(unittest.TestCase):
    def test_sampling_sees_masked_creation_attributes_and_controls_recording(self):
        sampler = CaptureSampler()
        provider = TracerProvider(sampler=sampler)
        tracer = FITracer(provider.get_tracer('test'), TraceConfig(hide_inputs=True))
        span = tracer.start_span('drop', attributes={'drop': True, 'input.value': 'secret'})
        self.assertFalse(span.is_recording())
        self.assertTrue(sampler.seen[0]['drop'])
        self.assertEqual(sampler.seen[0]['input.value'], '__REDACTED__')
        provider.shutdown()

    def test_async_suppression_and_restoration(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = FITracer(provider.get_tracer('test'))
        async def run():
            cm = suppress_tracing()
            async with cm:
                self.assertTrue(get_value(_SUPPRESS_INSTRUMENTATION_KEY))
                with tracer.start_as_current_span('suppressed'):
                    pass
                async with cm:
                    self.assertTrue(get_value(_SUPPRESS_INSTRUMENTATION_KEY))
                self.assertTrue(get_value(_SUPPRESS_INSTRUMENTATION_KEY))
            self.assertFalse(get_value(_SUPPRESS_INSTRUMENTATION_KEY))
            with tracer.start_as_current_span('recorded'):
                pass
        asyncio.run(run())
        self.assertEqual([span.name for span in exporter.get_finished_spans()], ['recorded'])
        provider.shutdown()

    def test_shared_suppression_is_task_local_and_exception_safe(self):
        async def run():
            cm = suppress_tracing()
            entered = asyncio.Event()
            release = asyncio.Event()
            async def suppressed():
                try:
                    async with cm:
                        entered.set()
                        await release.wait()
                        raise ValueError('expected')
                except ValueError:
                    pass
                self.assertFalse(get_value(_SUPPRESS_INSTRUMENTATION_KEY))
            task = asyncio.create_task(suppressed())
            await entered.wait()
            self.assertFalse(get_value(_SUPPRESS_INSTRUMENTATION_KEY))
            async with cm:
                self.assertTrue(get_value(_SUPPRESS_INSTRUMENTATION_KEY))
                release.set()
                await task
                self.assertTrue(get_value(_SUPPRESS_INSTRUMENTATION_KEY))
            self.assertFalse(get_value(_SUPPRESS_INSTRUMENTATION_KEY))
        asyncio.run(run())

    def test_signal_handler_is_not_replaced(self):
        previous = signal.getsignal(signal.SIGTERM)
        def handler(signum, frame):
            pass
        signal.signal(signal.SIGTERM, handler)
        provider = FITracerProvider()
        try:
            provider.setup_signal_handlers()
            self.assertIs(signal.getsignal(signal.SIGTERM), handler)
        finally:
            signal.signal(signal.SIGTERM, previous)
            provider.shutdown()

    def test_hidden_content_in_tool_prompt_and_retrieval_fields(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = FITracer(provider.get_tracer('test'), TraceConfig(hide_inputs=True, hide_outputs=True))
        sensitive = {
            'gen_ai.system_instructions': 'secret system',
            'gen_ai.prompts': 'secret prompt',
            'gen_ai.tool.definitions': 'secret schema',
            'gen_ai.tool.call.arguments': 'secret arguments',
            'gen_ai.tool.call.result': 'secret result',
            'gen_ai.retrieval.query': 'secret query',
            'retrieval.documents.0.document.content': 'secret document',
            'gen_ai.reranker.input_documents.0': 'secret rerank input',
            'gen_ai.reranker.output_documents.0': 'secret rerank output',
        }
        with tracer.start_as_current_span('test', attributes=sensitive):
            pass
        actual = exporter.get_finished_spans()[0].attributes
        for key, value in sensitive.items():
            with self.subTest(key=key):
                self.assertNotEqual(actual.get(key), value, key)
        provider.shutdown()

    def test_content_masks_are_directional_and_do_not_hide_metadata(self):
        input_config = TraceConfig(hide_inputs=True, hide_outputs=False)
        output_config = TraceConfig(hide_inputs=False, hide_outputs=True)
        self.assertEqual(input_config.mask('gen_ai.tool.call.arguments', 'secret'), '__REDACTED__')
        self.assertEqual(input_config.mask('gen_ai.tool.call.result', 'result'), 'result')
        self.assertEqual(output_config.mask('gen_ai.tool.call.result', 'secret'), '__REDACTED__')
        self.assertEqual(output_config.mask('gen_ai.tool.call.arguments', 'args'), 'args')
        self.assertEqual(input_config.mask('gen_ai.retrieval.query_extra', 'safe'), 'safe')
        self.assertEqual(output_config.mask('gen_ai.usage.output_tokens', 12), 12)

    def test_event_and_exception_attributes_obey_content_mask(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = FITracer(provider.get_tracer('test'), TraceConfig(hide_inputs=True, hide_error_details=False))
        with tracer.start_as_current_span('test') as span:
            span.add_event('custom', {'input.value': 'secret1'})
            span.record_exception(ValueError('ordinary'), {'input.value': 'secret2'})
        events = exporter.get_finished_spans()[0].events
        self.assertEqual(events[0].attributes['input.value'], '__REDACTED__')
        self.assertEqual(events[1].attributes['input.value'], '__REDACTED__')
        provider.shutdown()


if __name__ == '__main__':
    unittest.main()
