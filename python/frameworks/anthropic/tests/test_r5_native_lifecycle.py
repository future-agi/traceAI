import asyncio, json
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
DELTA={'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':2}}
PARAMS=dict(model='local',max_tokens=16,messages=[{'role':'user','content':'hi'}],stream=True)
def event(data): return ('event: '+data['type']+'\ndata: '+json.dumps(data)+'\n\n').encode()
def setup():
    p=TracerProvider(); x=InMemorySpanExporter(); p.add_span_processor(SimpleSpanProcessor(x)); i=AnthropicInstrumentor(); i.instrument(tracer_provider=p); return p,x,i

@pytest.mark.parametrize('traced',[False,True])
def test_close_during_advance_preserves_late_result_and_usage(traced):
    class Wire(httpx.AsyncByteStream):
        def __init__(self): self.pending=asyncio.Event(); self.release=asyncio.Event()
        async def __aiter__(self):
            yield event(START)
            self.pending.set(); await self.release.wait()
            yield event(DELTA)
        async def aclose(self): pass
    async def run():
        wire=Wire(); c=anthropic.AsyncAnthropic(api_key='local',http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=wire))))
        if traced: p,x,i=setup()
        try:
            s=await c.messages.create(**PARAMS); assert (await s.__anext__()).type=='message_start'
            pending=asyncio.create_task(s.__anext__()); await asyncio.wait_for(wire.pending.wait(),2)
            assert await asyncio.wait_for(s.close(),2) is None
            if traced: assert len(x.get_finished_spans())==0
            wire.release.set(); result=await asyncio.wait_for(pending,2); assert result.type=='message_delta' and result.usage.output_tokens==2
            if traced:
                spans=x.get_finished_spans(); assert len(spans)==1
                assert spans[0].attributes['gen_ai.usage.input_tokens']==4
                assert spans[0].attributes['gen_ai.usage.output_tokens']==2
                assert spans[0].status.status_code.name=='OK'
                await s.close(); assert len(x.get_finished_spans())==1
        finally:
            wire.release.set()
            if traced: i.uninstrument(); p.shutdown()
            await c.close()
    asyncio.run(run())

class NonStringifiableError(Exception):
    def __str__(self): raise RuntimeError('str failed')

@pytest.mark.parametrize('traced',[False,True])
def test_advance_preserves_exact_exception_even_if_stringification_fails(traced, monkeypatch):
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS','true')
    err=NonStringifiableError()
    class Wire(httpx.SyncByteStream):
        def __iter__(self):
            yield event(START)
            raise err
        def close(self): pass
    c=anthropic.Anthropic(api_key='local',http_client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Wire()))))
    if traced: p,x,i=setup()
    try:
        s=c.messages.create(**PARAMS); assert next(s).type=='message_start'
        try: next(s)
        except BaseException as got: assert got is err, f'expected original exception identity, got {type(got).__name__}'
        else: pytest.fail('expected transport error')
    finally:
        if traced: i.uninstrument(); p.shutdown()
        c.close()

@pytest.mark.parametrize('traced',[False,True])
def test_close_preserves_exact_exception_even_if_stringification_fails(traced, monkeypatch):
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS','true')
    err=NonStringifiableError()
    class Wire(httpx.SyncByteStream):
        def __iter__(self): yield event(START)
        def close(self): raise err
    c=anthropic.Anthropic(api_key='local',http_client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Wire()))))
    if traced: p,x,i=setup()
    try:
        s=c.messages.create(**PARAMS)
        try: s.close()
        except BaseException as got: assert got is err, f'expected original exception identity, got {type(got).__name__}'
        else: pytest.fail('expected transport error')
    finally:
        if traced: i.uninstrument(); p.shutdown()
        c.close()

@pytest.mark.parametrize('traced',[False,True])
def test_close_is_not_blocked_by_other_thread_export(traced):
    import threading
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    exporter_entered=threading.Event(); release=threading.Event(); close_done=threading.Event(); reader_errors=[]; close_errors=[]
    class BlockingExporter(SpanExporter):
        def export(self, spans):
            exporter_entered.set()
            if not release.wait(5): raise TimeoutError('test exporter timeout')
            return SpanExportResult.SUCCESS
        def shutdown(self): pass
    body=event(START)+event(DELTA)
    c=anthropic.Anthropic(api_key='local',http_client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},content=body))))
    if traced:
        p=TracerProvider(); p.add_span_processor(SimpleSpanProcessor(BlockingExporter())); i=AnthropicInstrumentor(); i.instrument(tracer_provider=p)
    try:
        s=c.messages.create(**PARAMS)
        def read():
            try: list(s)
            except BaseException as exc: reader_errors.append(exc)
            finally:
                if not traced: exporter_entered.set()
        def close():
            try: s.close()
            except BaseException as exc: close_errors.append(exc)
            finally: close_done.set()
        t=threading.Thread(target=read); t.start(); assert exporter_entered.wait(2)
        closer=threading.Thread(target=close); closer.start()
        returned=close_done.wait(0.5)
        release.set(); t.join(2); closer.join(2)
        assert not t.is_alive() and not closer.is_alive()
        assert not reader_errors and not close_errors
        assert returned, 'native close blocked behind tracing export'
    finally:
        release.set()
        if traced: i.uninstrument(); p.shutdown()
        c.close()

@pytest.mark.parametrize('hidden', [False, True])
@pytest.mark.parametrize('traced', [False, True])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_context_body_exception_identity_and_status(hidden, traced, asynchronous, monkeypatch):
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS', str(hidden).lower())
    err = NonStringifiableError()
    if asynchronous:
        async def run():
            c = anthropic.AsyncAnthropic(api_key='local', http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type':'text/event-stream'}, content=event(START)))))
            if traced: p,x,i=setup()
            try:
                s = await c.messages.create(**PARAMS)
                try:
                    async with s:
                        raise err
                except BaseException as got: assert got is err
                else: pytest.fail('expected body error')
                if traced:
                    spans=x.get_finished_spans(); assert len(spans)==1
                    assert spans[0].status.status_code.name == 'ERROR'
                    if hidden: assert 'NonStringifiableError' not in str(spans[0].status.description)
            finally:
                if traced: i.uninstrument(); p.shutdown()
                await c.close()
        asyncio.run(run()); return
    c = anthropic.Anthropic(api_key='local', http_client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type':'text/event-stream'}, content=event(START)))))
    if traced: p,x,i=setup()
    try:
        s=c.messages.create(**PARAMS)
        try:
            with s: raise err
        except BaseException as got: assert got is err
        else: pytest.fail('expected body error')
        if traced:
            spans=x.get_finished_spans(); assert len(spans)==1
            assert spans[0].status.status_code.name == 'ERROR'
            if hidden: assert 'NonStringifiableError' not in str(spans[0].status.description)
    finally:
        if traced: i.uninstrument(); p.shutdown()
        c.close()

@pytest.mark.parametrize('hidden', [False, True])
@pytest.mark.parametrize('traced', [False, True])
def test_request_error_identity(hidden, traced, monkeypatch):
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS', str(hidden).lower())
    err=NonStringifiableError()
    def transport(req): raise err
    c=anthropic.Anthropic(api_key='local', max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    if traced: p,x,i=setup()
    try:
        try: c.messages.create(**PARAMS)
        except BaseException as got:
            # The SDK wraps transport exceptions; compare its cause identity.
            assert got.__cause__ is err
        else: pytest.fail('expected transport error')
        if traced:
            spans=x.get_finished_spans(); assert len(spans)==1
            assert spans[0].status.status_code.name == 'ERROR'
    finally:
        if traced: i.uninstrument(); p.shutdown()
        c.close()

@pytest.mark.parametrize('hidden', [False, True])
@pytest.mark.parametrize('where', ['advance', 'close'])
@pytest.mark.parametrize('traced', [False, True])
def test_async_transport_exception_identity(hidden, where, traced, monkeypatch):
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS', str(hidden).lower())
    err = NonStringifiableError()
    class Wire(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event(START)
            if where == 'advance': raise err
        async def aclose(self):
            if where == 'close': raise err
    async def run():
        c=anthropic.AsyncAnthropic(api_key='local', http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Wire()))))
        if traced: p,x,i=setup()
        try:
            s=await c.messages.create(**PARAMS)
            if where == 'advance': assert (await s.__anext__()).type == 'message_start'
            try:
                if where == 'advance': await s.__anext__()
                else: await s.close()
            except BaseException as got: assert got is err
            else: pytest.fail('expected transport error')
            if traced:
                spans=x.get_finished_spans(); assert len(spans)==1
                assert spans[0].status.status_code.name=='ERROR'
                if hidden: assert 'NonStringifiableError' not in str(spans[0].status.description)
        finally:
            if traced: i.uninstrument(); p.shutdown()
            await c.close()
    asyncio.run(run())

@pytest.mark.parametrize('hidden', [False, True])
def test_ordinary_error_diagnostics_and_redaction(hidden, monkeypatch):
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS', str(hidden).lower())
    secret='private-error-diagnostic-r5'
    err=ValueError(secret)
    class Wire(httpx.SyncByteStream):
        def __iter__(self):
            yield event(START)
            raise err
        def close(self): pass
    c=anthropic.Anthropic(api_key='local',http_client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Wire()))))
    p,x,i=setup()
    try:
        s=c.messages.create(**PARAMS); assert next(s).type=='message_start'
        with pytest.raises(ValueError) as caught: next(s)
        assert caught.value is err
        spans=x.get_finished_spans(); assert len(spans)==1
        span=spans[0]; assert span.status.status_code.name=='ERROR'
        data=str(span.status.description)+str(span.events)+str(span.attributes)
        assert (secret not in data) if hidden else (secret in data)
    finally:
        i.uninstrument(); p.shutdown(); c.close()

def test_failing_exporter_does_not_replace_native_error():
    from opentelemetry import context as context_api
    from opentelemetry.sdk.trace.export import SpanExporter
    err = NonStringifiableError()
    class Wire(httpx.SyncByteStream):
        def __iter__(self):
            yield event(START)
            raise err
        def close(self): pass
    class BrokenExporter(SpanExporter):
        def export(self, spans): raise KeyboardInterrupt('export callback')
        def shutdown(self): pass
    p=TracerProvider(); p.add_span_processor(SimpleSpanProcessor(BrokenExporter()))
    i=AnthropicInstrumentor(); i.instrument(tracer_provider=p)
    c=anthropic.Anthropic(api_key='local',http_client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Wire()))))
    # Older OTel processors leave instrumentation suppressed if an exporter
    # raises BaseException. Isolate this deliberate failure from later tests.
    token = context_api.attach(context_api.get_current())
    try:
        s=c.messages.create(**PARAMS); assert next(s).type=='message_start'
        try: next(s)
        except BaseException as got: assert got is err
        else: pytest.fail('expected native error')
    finally:
        context_api.detach(token)
        i.uninstrument(); c.close(); p.shutdown()
