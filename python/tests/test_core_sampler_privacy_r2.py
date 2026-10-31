"""Regression tests against real SDK samplers and processor lifecycle."""
import unittest

from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import TraceState

from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig


class Capture(SpanProcessor):
    def __init__(self):
        self.starts = []
        self.ends = []

    def on_start(self, span, parent_context=None):
        self.starts.append(dict(span.attributes))

    def on_end(self, span):
        self.ends.append((dict(span.attributes), [dict(e.attributes) for e in span.events]))


class SelectingSampler(Sampler):
    def __init__(self, decision, attributes):
        self.decision = decision
        self.attributes = attributes
        self.seen = None

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
        self.seen = dict(attributes or {})
        return SamplingResult(self.decision, self.attributes, TraceState([('vendor', 'state')]))

    def get_description(self):
        return 'selection-test'


class SamplerPrivacy(unittest.TestCase):
    def test_raw_fi_parity_all_decisions(self):
        for decision in (Decision.DROP, Decision.RECORD_ONLY, Decision.RECORD_AND_SAMPLE):
            with self.subTest(decision=decision):
                def run(fi):
                    sampler = SelectingSampler(decision, {'choice': 'sampler', 'sampler-only': 'safe'})
                    provider = TracerProvider(sampler=sampler)
                    capture = Capture()
                    provider.add_span_processor(capture)
                    tracer = provider.get_tracer('test')
                    if fi:
                        tracer = FITracer(tracer)
                    span = tracer.start_span('test', attributes={'choice': 'caller', 'removed': 'caller'})
                    state = span.get_span_context().trace_state.to_header()
                    sampled = span.get_span_context().trace_flags.sampled
                    recording = span.is_recording()
                    span.end()
                    provider.shutdown()
                    return sampler.seen, state, sampled, recording, capture.starts, capture.ends
                self.assertEqual(run(False), run(True))

    def test_sampler_generated_secrets_hidden_before_start(self):
        for decision in (Decision.DROP, Decision.RECORD_ONLY, Decision.RECORD_AND_SAMPLE):
            with self.subTest(decision=decision):
                sampler = SelectingSampler(decision, {'input.value': 'SAMPLER_SECRET', 'gen_ai.tool.call': 'SAMPLER_TOOL_SECRET', 'metadata': 'preserved'})
                provider = TracerProvider(sampler=sampler)
                capture = Capture()
                provider.add_span_processor(capture)
                tracer = FITracer(provider.get_tracer('test'), TraceConfig(hide_inputs=True))
                span = tracer.start_span('test', attributes={'input.value': 'CALLER_SECRET', 'metadata': 'caller'})
                self.assertEqual(sampler.seen['input.value'], '__REDACTED__')
                self.assertEqual(span.get_span_context().trace_state.to_header(), 'vendor=state')
                span.end()
                provider.shutdown()
                self.assertNotIn('SECRET', repr((capture.starts, capture.ends)))
                if decision is not Decision.DROP:
                    self.assertEqual(capture.starts[0]['metadata'], 'preserved')
                    self.assertEqual(capture.starts[0]['input.value'], '__REDACTED__')
                    self.assertEqual(capture.starts[0]['gen_ai.tool.call'], '__REDACTED__')

    def test_decorator_with_omitted_kind_remains_callable_without_restoring_kind(self):
        for decision in (Decision.DROP, Decision.RECORD_ONLY, Decision.RECORD_AND_SAMPLE):
            with self.subTest(decision=decision):
                provider = TracerProvider(sampler=SelectingSampler(decision, {'metadata': 'selected'}))
                capture = Capture()
                provider.add_span_processor(capture)
                tracer = FITracer(provider.get_tracer('test'))
                @tracer.chain
                def double(value):
                    return value * 2
                self.assertEqual(double(3), 6)
                provider.shutdown()
                if decision is not Decision.DROP:
                    self.assertNotIn(SpanAttributes.GEN_AI_SPAN_KIND, capture.ends[0][0])
                    self.assertEqual(capture.ends[0][0]['metadata'], 'selected')

    def test_sampler_selected_kind_not_overwritten(self):
        from fi_instrumentation.fi_types import FiSpanKindValues
        kind = SpanAttributes.GEN_AI_SPAN_KIND
        provider = TracerProvider(sampler=SelectingSampler(Decision.RECORD_AND_SAMPLE, {kind: 'SAMPLER'}))
        capture = Capture()
        provider.add_span_processor(capture)
        tracer = FITracer(provider.get_tracer('test'))
        span = tracer.start_span('test', fi_span_kind=FiSpanKindValues.CHAIN)
        span.end()
        provider.shutdown()
        self.assertEqual(capture.starts[0][kind], 'SAMPLER')
        self.assertEqual(capture.ends[0][0][kind], 'SAMPLER')

    def test_selected_kind_survives_attribute_limit_without_resurrecting_omitted_kind(self):
        from opentelemetry.sdk.trace import SpanLimits
        kind = SpanAttributes.GEN_AI_SPAN_KIND
        for selected in (True, False):
            with self.subTest(selected=selected):
                attrs = {kind: 'SELECTED', 'other': 'value'} if selected else {'other': 'value'}
                provider = TracerProvider(sampler=SelectingSampler(Decision.RECORD_AND_SAMPLE, attrs), span_limits=SpanLimits(max_attributes=1))
                capture = Capture()
                provider.add_span_processor(capture)
                tracer = FITracer(provider.get_tracer('test'))
                span = tracer.start_span('test', attributes={kind: 'CALLER'})
                span.end()
                provider.shutdown()
                self.assertEqual(capture.ends[0][0].get(kind), 'SELECTED' if selected else None)

    def test_hide_input_images_covers_public_image_field(self):
        config = TraceConfig(hide_input_images=True)
        self.assertIsNone(config.mask(SpanAttributes.INPUT_IMAGES, 'IMAGE_SECRET'))
        self.assertIsNone(config.mask(SpanAttributes.INPUT_IMAGES + '.0', 'IMAGE_SECRET'))
        self.assertEqual(config.mask(SpanAttributes.GEN_AI_IMAGE_PROMPT, 'prompt'), 'prompt')

    def test_directional_content_at_creation_setters_and_events(self):
        inputs = (SpanAttributes.GEN_AI_IMAGE_PROMPT, SpanAttributes.GEN_AI_IMAGE_NEGATIVE_PROMPT, SpanAttributes.INPUT_IMAGES)
        outputs = (SpanAttributes.GEN_AI_IMAGE_OUTPUT_URLS,)
        aggregate = SpanAttributes.GEN_AI_TOOL_CALL
        for hide_inputs, hide_outputs in ((True, False), (False, True), (True, True), (False, False)):
            with self.subTest(hide_inputs=hide_inputs, hide_outputs=hide_outputs):
                values = {key: 'content-' + key for key in (*inputs, *outputs, aggregate)}
                values['gen_ai.tool.name'] = 'safe-tool'
                sampler = SelectingSampler(Decision.RECORD_AND_SAMPLE, values)
                provider = TracerProvider(sampler=sampler)
                capture = Capture()
                provider.add_span_processor(capture)
                tracer = FITracer(provider.get_tracer('test'), TraceConfig(hide_inputs=hide_inputs, hide_outputs=hide_outputs))
                span = tracer.start_span('test', attributes=values)
                for key, value in values.items():
                    span.set_attribute(key, value)
                span.add_event('event', values)
                span.end()
                provider.shutdown()
                for actual in (sampler.seen, capture.starts[0], capture.ends[0][0], capture.ends[0][1][0]):
                    for key in inputs:
                        self.assertEqual(actual[key], '__REDACTED__' if hide_inputs else values[key], key)
                    for key in outputs:
                        self.assertEqual(actual[key], '__REDACTED__' if hide_outputs else values[key], key)
                    self.assertEqual(actual[aggregate], '__REDACTED__' if hide_inputs or hide_outputs else values[aggregate])
                    self.assertEqual(actual['gen_ai.tool.name'], 'safe-tool')


if __name__ == '__main__':
    unittest.main()
