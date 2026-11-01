import json, threading
import anthropic, pytest
try:
    import httpx2 as httpx
except ImportError:
    import httpx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_anthropic import AnthropicInstrumentor

START={'type':'message_start','message':{'id':'msg_1','type':'message','role':'assistant','content':[],'model':'local','stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':4,'output_tokens':0}}}
BLOCK={'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}
TEXT={'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'hello'}}
PARAMS=dict(model='local',max_tokens=16,messages=[{'role':'user','content':'hi'}],stream=True)
def event(data): return ('event: '+data['type']+'\ndata: '+json.dumps(data)+'\n\n').encode()
def client(body): return anthropic.Anthropic(api_key='local',http_client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},content=body))))
def setup():
    p=TracerProvider(); x=InMemorySpanExporter(); p.add_span_processor(SimpleSpanProcessor(x)); i=AnthropicInstrumentor(); i.instrument(tracer_provider=p); return p,x,i

@pytest.mark.parametrize('traced',[False,True])
def test_event_with_null_usage_reaches_caller_and_rest_is_traced(traced):
    # The SDK accepts a message_start whose usage is null; telemetry must not replace it.
    start=json.loads(json.dumps(START)); start['message']['usage']=None
    c=client(event(start)+event(BLOCK)+event(TEXT))
    if traced: p,x,i=setup()
    try:
        s=c.messages.create(**PARAMS)
        first=next(s); assert first.type=='message_start' and first.message.usage is None
        assert [e.type for e in s]==['content_block_start','content_block_delta']
        assert s.close() is None
        if traced:
            spans=x.get_finished_spans(); assert len(spans)==1
            assert spans[0].status.status_code.name!='ERROR'
            assert spans[0].attributes['gen_ai.output.messages.0.message.role']=='assistant'
            assert spans[0].attributes['gen_ai.output.messages.0.message.content']=='hello'
            assert spans[0].attributes['gen_ai.usage.input_tokens']==0
    finally:
        if traced: i.uninstrument(); p.shutdown()
        c.close()

def test_unreadable_event_keeps_native_item_and_later_telemetry(monkeypatch):
    from anthropic.types.raw_message_start_event import RawMessageStartEvent
    class Broken(Exception): pass
    def role(self): raise Broken()
    c=client(event(START)+event(BLOCK)+event(TEXT))
    p,x,i=setup()
    try:
        s=c.messages.create(**PARAMS)
        monkeypatch.setattr(RawMessageStartEvent,'message',property(role),raising=False)
        first=next(s)
        monkeypatch.undo()
        assert first.type=='message_start'
        assert [e.type for e in s]==['content_block_start','content_block_delta']
        assert s.close() is None
        spans=x.get_finished_spans(); assert len(spans)==1
        assert spans[0].status.status_code.name!='ERROR'
        assert 'hello' in spans[0].attributes['output.value']
    finally:
        i.uninstrument(); p.shutdown(); c.close()

@pytest.mark.parametrize('traced',[False,True])
def test_sdk_serializer_runs_outside_lifecycle_lock(monkeypatch,traced):
    try:
        from anthropic.types import Completion
    except ImportError:
        pytest.skip('native SDK removed completions')
    c=client(b'event: completion\ndata: {"id":"c1","type":"completion","model":"local","completion":"hello","stop_reason":"stop_sequence"}\n\n')
    if not hasattr(c,'completions'):
        c.close(); pytest.skip('native SDK removed completions')
    original=Completion.model_dump; acquired=[]; holder={}
    def model_dump(self,*args,**kwargs):
        def probe():
            got=holder['stream']._self_lock.acquire(timeout=0.5); acquired.append(got)
            if got: holder['stream']._self_lock.release()
        t=threading.Thread(target=probe); t.start(); t.join(2)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Completion,'model_dump',model_dump)
    if traced: p,x,i=setup()
    try:
        s=c.completions.create(model='local',prompt='Hi',max_tokens_to_sample=8,stream=True); holder['stream']=s
        assert next(s).completion=='hello'
        assert acquired==([True] if traced else [])
        assert s.close() is None
        if traced:
            spans=x.get_finished_spans(); assert len(spans)==1
            assert 'hello' in spans[0].attributes['output.value']
    finally:
        if traced: i.uninstrument(); p.shutdown()
        c.close()
