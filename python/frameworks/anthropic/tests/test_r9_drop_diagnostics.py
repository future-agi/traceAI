import json, logging
import anthropic, pytest
try:
    import httpx2 as httpx
except ImportError:
    import httpx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_anthropic import AnthropicInstrumentor
from traceai_anthropic import _stream

START={'type':'message_start','message':{'id':'msg_1','type':'message','role':'assistant','content':[],'model':'local','stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':4,'output_tokens':0}}}
BLOCK={'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}
TEXT={'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'hello'}}
PARAMS=dict(model='local',max_tokens=16,messages=[{'role':'user','content':'hi'}],stream=True)
SECRET='model content that must not be logged'
def event(data): return ('event: '+data['type']+'\ndata: '+json.dumps(data)+'\n\n').encode()
def client(body): return anthropic.Anthropic(api_key='local',http_client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},content=body))))
def setup():
    p=TracerProvider(); x=InMemorySpanExporter(); p.add_span_processor(SimpleSpanProcessor(x)); i=AnthropicInstrumentor(); i.instrument(tracer_provider=p); return p,x,i
def drops(caplog): return [r for r in caplog.records if r.name==_stream.logger.name]

@pytest.mark.parametrize('stage',['extract','merge'])
def test_dropped_stream_telemetry_warns_once_without_content(monkeypatch,caplog,stage):
    # A telemetry failure keeps the native stream intact; it is reported once per
    # stream at WARNING with the exception type only, then at DEBUG.
    class Broken(Exception): pass
    def fail(self,*args,**kwargs): raise Broken(SECRET)
    target=_stream._MessageResponseAccumulator
    c=client(event(START)+event(BLOCK)+event(TEXT))
    p,x,i=setup()
    caplog.set_level(logging.DEBUG,logger=_stream.logger.name)
    try:
        monkeypatch.setattr(target,stage,fail)
        s=c.messages.create(**PARAMS)
        assert [e.type for e in s]==['message_start','content_block_start','content_block_delta']
        assert s.close() is None
        records=drops(caplog)
        assert [r.levelno for r in records]==[logging.WARNING,logging.DEBUG,logging.DEBUG]
        assert all('Broken' in r.getMessage() for r in records)
        assert not any(SECRET in r.getMessage() for r in records)
        assert len(x.get_finished_spans())==1
    finally:
        monkeypatch.undo(); i.uninstrument(); p.shutdown(); c.close()

def test_healthy_stream_logs_nothing(caplog):
    c=client(event(START)+event(BLOCK)+event(TEXT))
    p,x,i=setup()
    caplog.set_level(logging.DEBUG,logger=_stream.logger.name)
    try:
        s=c.messages.create(**PARAMS)
        assert [e.type for e in s]==['message_start','content_block_start','content_block_delta']
        assert s.close() is None
        assert drops(caplog)==[]
    finally:
        i.uninstrument(); p.shutdown(); c.close()

def test_stalled_extraction_is_reported(caplog):
    # Giving up on stalled earlier events is reported, without any event content.
    stream=_stream._StreamLifecycle(iter(()))
    caplog.set_level(logging.DEBUG,logger=_stream.logger.name)
    stream._self_extracting={0}
    stream._self_next_ticket=_stream._MAX_PENDING+2
    for ticket in range(1,_stream._MAX_PENDING+2):
        stream._self_pending[ticket]=None
    stream._self_active=1
    stream._settle(False,None,_stream._MAX_PENDING+2,None)
    records=drops(caplog)
    assert len(records)==1 and records[0].levelno==logging.WARNING
    assert 'stopped waiting for 1 stalled stream event(s)' in records[0].getMessage()
