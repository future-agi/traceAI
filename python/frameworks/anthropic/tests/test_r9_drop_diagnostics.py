import inspect, json, logging, sys, threading
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
    assert drops(caplog)==[], 'nothing is logged while the lifecycle lock may be held'
    stream._report_drops()
    records=drops(caplog)
    assert len(records)==1 and records[0].levelno==logging.WARNING
    assert 'stopped waiting for 1 stalled stream event(s)' in records[0].getMessage()

@pytest.mark.parametrize('stage',['extract','merge'])
def test_failing_log_handler_cannot_replace_native_item(monkeypatch,stage):
    class FailingHandler(logging.Handler):
        def emit(self,record): raise RuntimeError('synthetic logging sink failure')
    def fail(self,*args,**kwargs): raise ValueError(SECRET)
    c=client(event(START)+event(BLOCK)+event(TEXT))
    p,x,i=setup(); h=FailingHandler(); _stream.logger.addHandler(h); s=None
    try:
        monkeypatch.setattr(_stream._MessageResponseAccumulator,stage,fail)
        s=c.messages.create(**PARAMS)
        assert [e.type for e in s]==['message_start','content_block_start','content_block_delta']
    finally:
        _stream.logger.removeHandler(h); monkeypatch.undo()
        if s is not None: s.close()
        i.uninstrument(); p.shutdown(); c.close()

def test_log_handler_runs_outside_the_lifecycle_lock(monkeypatch):
    # A handler that waits for another thread to close the stream must not deadlock.
    c=client(event(START)+event(BLOCK)+event(TEXT))
    p,x,i=setup(); s=c.messages.create(**PARAMS)
    closed=threading.Event(); waited=[]; threads=[]
    native_close=s.__wrapped__.close
    def close():
        native_close(); closed.set()
    monkeypatch.setattr(s.__wrapped__,'close',close)
    def fail(self,*args,**kwargs): raise ValueError(SECRET)
    monkeypatch.setattr(_stream._MessageResponseAccumulator,'merge',fail)
    class CloseHandler(logging.Handler):
        def emit(self,record):
            if threads: return
            t=threading.Thread(target=s.close,daemon=True); threads.append(t); t.start()
            waited.append(closed.wait(2))
    h=CloseHandler(); _stream.logger.addHandler(h)
    try:
        assert next(s).type=='message_start'
        for t in threads: t.join(2)
        assert waited==[True]
    finally:
        _stream.logger.removeHandler(h); monkeypatch.undo(); s.close()
        i.uninstrument(); p.shutdown(); c.close()

def test_warning_claim_is_atomic_between_threads():
    # Pause one thread inside the claim; a second drop meanwhile must not also warn.
    stream=_stream._StreamLifecycle(iter(()))
    code=_stream._StreamLifecycle._dropped.__code__
    lines,start=inspect.getsourcelines(_stream._StreamLifecycle._dropped)
    line=start+next(n for n,text in enumerate(lines) if 'self._self_dropped = True' in text)
    paused,resume=threading.Event(),threading.Event()
    def trace(frame,event_name,arg):
        if event_name=='line' and frame.f_code is code and frame.f_lineno==line and not paused.is_set():
            paused.set(); resume.wait(2)
        return trace
    def first():
        sys.settrace(trace)
        try: stream._dropped('could not read a stream event',ValueError('synthetic'))
        finally: sys.settrace(None)
    a=threading.Thread(target=first); a.start(); assert paused.wait(2)
    b=threading.Thread(target=stream._dropped,args=('could not record a stream event',ValueError('synthetic'))); b.start()
    b.join(0.3)
    resume.set(); a.join(2); b.join(2)
    assert sorted(level for level,_ in stream._self_drop_reports)==[logging.DEBUG,logging.WARNING]
