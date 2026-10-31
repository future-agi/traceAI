"""Privacy and decorator regression contract; runs against installed artifacts."""
import asyncio
import pytest
from opentelemetry import context as ot_context
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider, SpanLimits
from opentelemetry.sdk.trace.sampling import Sampler, SamplingResult, Decision
from opentelemetry.trace import Link, SpanContext, TraceFlags, TraceState, NonRecordingSpan, set_span_in_context
from fi_instrumentation.fi_types import SpanAttributes as A, FiSpanKindValues
from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig, suppress_tracing

INPUT = [A.INPUT_VALUE, A.RAW_INPUT, A.LLM_PROMPT_TEMPLATE_VARIABLES, A.GEN_AI_PROMPT_TEMPLATE_VARIABLES, A.GEN_AI_REQUEST_PARAMETERS, A.GEN_AI_SYSTEM_INSTRUCTIONS, A.GEN_AI_IMAGE_PROMPT, A.GEN_AI_IMAGE_NEGATIVE_PROMPT, A.INPUT_IMAGES, A.GEN_AI_PROMPTS, A.GEN_AI_TOOL_DEFINITIONS, A.GEN_AI_TOOL_DESCRIPTION, A.GEN_AI_TOOL_CALL_ARGUMENTS, A.TOOL_PARAMETERS, A.GEN_AI_RETRIEVAL_QUERY, A.GEN_AI_RERANKER_QUERY, A.GEN_AI_RERANKER_INPUT_DOCUMENTS, A.GEN_AI_REQUEST_STOP_SEQUENCES, A.GEN_AI_AGENT_DESCRIPTION, A.GEN_AI_COMPUTER_USE_TEXT, A.GEN_AI_COMPUTER_USE_ELEMENT_SELECTOR, 'tool_call.function.arguments', 'tool.json_schema', 'message.function_call_arguments_json', 'reranker.query', 'reranker.input_documents']
OUTPUT = [A.OUTPUT_VALUE, A.RAW_OUTPUT, A.GEN_AI_TOOL_CALL_RESULT, A.GEN_AI_IMAGE_REVISED_PROMPT, A.GEN_AI_IMAGE_OUTPUT_URLS, A.RETRIEVAL_DOCUMENTS, A.GEN_AI_RETRIEVAL_DOCUMENTS, A.GEN_AI_RERANKER_OUTPUT_DOCUMENTS, A.GEN_AI_GUARDRAIL_MODIFIED_OUTPUT, A.GEN_AI_EVALUATION_EXPLANATION, A.GEN_AI_COMPUTER_USE_RESULT, 'reranker.output_documents']
MIXED = [A.GEN_AI_TOOL_CALL, A.GEN_AI_AUDIO_URL, A.GEN_AI_AUDIO_TRANSCRIPT, A.GEN_AI_VOICE_TRANSCRIPT, A.GEN_AI_VOICE_RECORDING_URL, A.GEN_AI_VOICE_RECORDING_STEREO_URL, A.GEN_AI_VOICE_RECORDING_CUSTOMER_URL, A.GEN_AI_VOICE_RECORDING_ASSISTANT_URL, A.GEN_AI_COMPUTER_USE_SCREENSHOT, A.GEN_AI_COMPUTER_USE_CURRENT_URL]
SAFE = [A.GEN_AI_TOOL_NAME, A.GEN_AI_TOOL_CALL_ID, A.GEN_AI_REQUEST_MODEL, A.GEN_AI_USAGE_INPUT_TOKENS, A.GEN_AI_IMAGE_WIDTH, A.GEN_AI_IMAGE_HEIGHT, A.GEN_AI_AUDIO_DURATION_SECS]

@pytest.mark.parametrize('key,direction', [(k,'input') for k in INPUT] + [(k,'output') for k in OUTPUT] + [(k,'mixed') for k in MIXED] + [(k,'safe') for k in SAFE])
@pytest.mark.parametrize('hi,ho', [(False,False),(True,False),(False,True),(True,True)])
def test_content_metadata_contract(key, direction, hi, ho):
    c = TraceConfig(hide_inputs=hi, hide_outputs=ho)
    expected = '__REDACTED__' if (hi and direction in ('input','mixed')) or (ho and direction in ('output','mixed')) else 'secret'
    assert c.mask(key, 'secret') == expected
    # The serialized aggregate tool call is not a prefix: its safe ID is separate.
    if key != A.GEN_AI_TOOL_CALL and direction != 'safe':
        assert c.mask(key + '.child', 'secret') == expected

class SamplerCapture(Sampler):
    def __init__(self, decision): self.decision = decision; self.calls = []
    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
        self.calls.append((parent_context, dict(attributes or {}), [(link.context, dict(link.attributes or {})) for link in (links or [])]))
        return SamplingResult(self.decision, {A.GEN_AI_SPAN_KIND: 'CHAIN', 'input.value': 'sampler-secret'}, TraceState([('sample','ok')]))
    def get_description(self): return 'capture'

class Capture(SpanProcessor):
    def __init__(self): self.starts=[]; self.ends=[]
    def on_start(self, span, parent_context=None): self.starts.append((dict(span.attributes), [(x.context, dict(x.attributes or {})) for x in span.links]))
    def on_end(self, span): self.ends.append(span)

@pytest.mark.parametrize('decision', [Decision.DROP, Decision.RECORD_ONLY, Decision.RECORD_AND_SAMPLE])
def test_sampler_links_and_logical_kind(decision):
    sampler=SamplerCapture(decision); p=TracerProvider(sampler=sampler, span_limits=SpanLimits(max_attributes=2)); cap=Capture(); p.add_span_processor(cap)
    t=FITracer(p.get_tracer('r3'), TraceConfig(hide_inputs=True,hide_outputs=True))
    linkctx=SpanContext(12,13,True,TraceFlags(1),TraceState([('linked','state')]))
    attrs={'input.value':'link-secret', A.GEN_AI_IMAGE_REVISED_PROMPT:'link-secret', 'safe.id':'abc'}
    link=Link(linkctx,attrs)
    parent=SpanContext(100,101,True,TraceFlags(1),TraceState([('parent','yes')]))
    ctx=set_span_in_context(NonRecordingSpan(parent))
    s=t.start_span('tool',context=ctx,fi_span_kind=FiSpanKindValues.TOOL,links=[link],attributes={'input.value':'secret'})
    assert sampler.calls[0][0] is ctx
    sampled_context, sampled_attributes=sampler.calls[0][2][0]
    assert sampled_context == linkctx and sampled_context.trace_state.to_header() == 'linked=state'
    assert sampled_attributes['input.value'] == '__REDACTED__'
    assert sampled_attributes[A.GEN_AI_IMAGE_REVISED_PROMPT] == '__REDACTED__'
    assert sampled_attributes['safe.id'] == 'abc' and attrs['input.value'] == 'link-secret'
    assert s.get_span_context().trace_state.to_header() == 'sample=ok'
    assert s.get_span_context().trace_flags.sampled == decision.is_sampled()
    s.set_tool(name='helper',parameters={})
    if hasattr(s.__wrapped__, 'add_link'):
        s.add_link(linkctx, attrs)
    s.end()
    if decision.is_recording():
        assert cap.ends[0].attributes[A.GEN_AI_SPAN_KIND] == 'CHAIN'
        assert cap.starts[0][0].get('input.value') != 'sampler-secret'
        assert 'link-secret' not in repr(cap.starts[0][1])
        for item in cap.ends[0].links:
            assert 'link-secret' not in repr(item.attributes)
            assert item.context == linkctx
    else:
        assert not cap.starts and not cap.ends
    p.shutdown()

def test_revised_prompt_and_post_start_link_surfaces():
    p=TracerProvider(); cap=Capture(); p.add_span_processor(cap)
    t=FITracer(p.get_tracer('r3'), TraceConfig(hide_outputs=True, hide_inputs=True))
    linkctx=SpanContext(12,13,True,TraceFlags(1),TraceState([('linked','state')]))
    fields={A.GEN_AI_IMAGE_REVISED_PROMPT:'secret', A.GEN_AI_IMAGE_REVISED_PROMPT+'.child':'secret', 'input.value':'secret', A.GEN_AI_IMAGE_WIDTH:512}
    original=dict(fields); link=Link(linkctx,fields)
    s=t.start_span('surfaces',attributes=fields,links=[link])
    s.set_attributes(fields); s.add_event('event',fields); s.add_link(linkctx,fields); s.end()
    assert fields == original and dict(link.attributes) == original
    assert len(cap.ends[0].links) == 2
    assert cap.ends[0].links[1].context == linkctx
    for attrs in [cap.starts[0][0],cap.starts[0][1][0][1],dict(cap.ends[0].attributes),dict(cap.ends[0].events[0].attributes), *[dict(x.attributes) for x in cap.ends[0].links]]:
        assert attrs[A.GEN_AI_IMAGE_REVISED_PROMPT] == '__REDACTED__'
        assert attrs[A.GEN_AI_IMAGE_REVISED_PROMPT+'.child'] == '__REDACTED__'
        assert attrs['input.value'] == '__REDACTED__'
        assert attrs[A.GEN_AI_IMAGE_WIDTH] == 512
    p.shutdown()

@pytest.mark.parametrize('decision', [Decision.DROP, Decision.RECORD_ONLY, Decision.RECORD_AND_SAMPLE])
def test_suppressed_decorators_and_errors(decision):
    sampler=SamplerCapture(decision); p=TracerProvider(sampler=sampler); cap=Capture(); p.add_span_processor(cap); t=FITracer(p.get_tracer('r3'))
    async def run():
        for decorator in (t.chain, t.agent, t.tool):
            def value(x): return x + 1
            def failure(x): raise LookupError('original')
            async def avalue(x): await asyncio.sleep(0); return x+1
            async def afailure(x): await asyncio.sleep(0); raise LookupError('original')
            f, bad, af, abad = map(decorator, (value, failure, avalue, afailure))
            async with suppress_tracing():
                with suppress_tracing():
                    assert f(1) == 2 and await af(1) == 2
                    with pytest.raises(LookupError, match='original'): bad(1)
                    with pytest.raises(LookupError, match='original'): await abad(1)
        async def child():
            async with suppress_tracing():
                await asyncio.sleep(0)
                assert t.chain(lambda x:x)(9) == 9
        await asyncio.gather(child(), child())
    asyncio.run(run())
    assert not sampler.calls and not cap.starts and not cap.ends
    p.shutdown()
