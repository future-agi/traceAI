"""Native SDK sampler validation parity and privacy regressions."""
import logging

import pytest
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import Link, SpanContext, TraceFlags, TraceState

from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig


class Key:
    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text


class PlainKey:
    pass


class Select(Sampler):
    def __init__(self, attrs):
        self.attrs = attrs

    def should_sample(self, *args, **kwargs):
        return SamplingResult(Decision.RECORD_AND_SAMPLE, self.attrs, TraceState([('v', 's')]))

    def get_description(self):
        return 'test'


def run(attrs, fi):
    provider = TracerProvider(sampler=Select(attrs), shutdown_on_exit=False)
    tracer = provider.get_tracer('key-test')
    if fi:
        tracer = FITracer(tracer, TraceConfig(hide_inputs=True))
    span = tracer.start_span('test')
    state = span.get_span_context().trace_state.to_header()
    result = dict(span.attributes)
    span.end()
    provider.shutdown()
    return result, state


@pytest.mark.parametrize('key', [None, '', 0, 42, False, True, b'', b'bytes', Key('safe.normalized'), PlainKey()])
def test_malformed_keys_follow_installed_sdk(key, caplog):
    attrs = {key: 'value', 'safe': 'metadata'}
    with caplog.at_level(logging.WARNING):
        raw, raw_state = run(attrs, False)
        caplog.clear()
        actual, state = run(attrs, True)
    assert actual == raw
    assert state == raw_state == 'v=s'
    assert caplog.records


@pytest.mark.parametrize('order', [0, 1])
def test_normalized_sensitive_collision_is_masked(order, caplog):
    key = Key('input.value')
    pairs = [(key, 'CUSTOM_SECRET'), ('input.value', 'STRING_SECRET')]
    if order:
        pairs.reverse()
    attrs = dict(pairs + [('safe', 'metadata')])
    raw, _ = run(attrs, False)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        actual, _ = run(attrs, True)
    assert actual['input.value'] == '__REDACTED__'
    assert actual['safe'] == raw['safe'] == 'metadata'
    assert 'SECRET' not in repr(actual)
    assert 'SECRET' not in caplog.text


@pytest.mark.parametrize('order', [0, 1])
def test_normalized_safe_collision_matches_native(order):
    pairs = [(Key('duplicate'), 'normalized'), ('duplicate', 'literal')]
    if order:
        pairs.reverse()
    attrs = dict(pairs)
    raw, _ = run(attrs, False)
    actual, _ = run(attrs, True)
    assert actual == raw


def test_untrusted_key_not_disclosed_in_sdk_warning(caplog):
    with caplog.at_level(logging.WARNING):
        actual, _ = run({Key('input.value'): 'VALUE_SECRET', None: 'OTHER_SECRET'}, True)
    assert 'VALUE_SECRET' not in caplog.text
    assert 'OTHER_SECRET' not in caplog.text
    assert 'input.value' not in caplog.text
    assert 'SECRET' not in repr(actual)


def test_sampler_none_value_uses_native_validation_without_bypassing_policy():
    raw, _ = run({'safe.none': None}, False)
    actual, _ = run({'safe.none': None}, True)
    assert actual == raw
    actual, _ = run({'input.value': None, 'safe': 'ok'}, True)
    assert actual['input.value'] == '__REDACTED__'
    assert actual['safe'] == 'ok'


def test_sampler_callable_is_not_executed():
    def bad():
        raise AssertionError('SDK sampler values are not lazy callbacks')
    actual, _ = run({'input.value': bad, 'safe': 'ok'}, True)
    assert actual['input.value'] == '__REDACTED__'
    assert actual['safe'] == 'ok'


class Capture(SpanProcessor):
    def __init__(self):
        self.start = None
        self.end = None

    def on_start(self, span, parent_context=None):
        self.start = (dict(span.attributes), [dict(link.attributes or {}) for link in span.links])

    def on_end(self, span):
        self.end = span


def test_sampler_vectors_and_all_native_surfaces():
    vector_key = SpanAttributes.GEN_AI_EMBEDDINGS_VECTORS
    raw = {vector_key: [0.25, 0.75], 'safe': 'metadata'}
    provider = TracerProvider(sampler=Select(dict(raw)), shutdown_on_exit=False)
    capture = Capture()
    provider.add_span_processor(capture)
    ctx = SpanContext(12, 13, False, TraceFlags(1), TraceState())
    link = Link(ctx, dict(raw))
    tracer = FITracer(provider.get_tracer('embed'), TraceConfig(hide_embedding_vectors=True))
    span = tracer.start_span('embedding', attributes=raw, links=[link])
    span.set_attributes(raw)
    span.add_event('event', raw)
    span.add_link(ctx, raw)
    span.end()
    provider.shutdown()
    surfaces = [capture.start[0], capture.start[1][0], capture.end.attributes,
                capture.end.events[0].attributes, capture.end.links[1].attributes]
    for surface in surfaces:
        assert vector_key not in surface
        assert surface['safe'] == 'metadata'
    assert raw[vector_key] == [0.25, 0.75]
    assert link.attributes[vector_key] == [0.25, 0.75]
