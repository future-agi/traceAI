"""Offline contracts for request boundaries, never client serialization."""

import asyncio
import json
from importlib import import_module
from importlib.metadata import version

import instructor
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from traceai_instructor import InstructorInstrumentor

httpx = import_module("httpx2" if int(version("openai").split(".")[0]) >= 3 else "httpx")
FAKE_KEY = "synthetic-instructor-key-not-real"
CURRENT = tuple(map(int, version("instructor").split(".")[:2])) >= (1, 17)


class Person(BaseModel):
    name: str
    age: int


PARAMS = dict(model="offline-model", response_model=Person,
              messages=[{"role": "user", "content": "Ada is 37"}])


@pytest.fixture(autouse=True)
def fresh_messages():
    # Older Instructor versions mutate the caller's messages during preparation.
    PARAMS["messages"] = [{"role": "user", "content": "Ada is 37"}]


def completion(age=37):
    return {"id": "offline", "object": "chat.completion", "created": 1,
            "model": "offline-model", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": json.dumps({"name": "Ada", "age": age})},
            "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}


@pytest.fixture
def traces():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = InstructorInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield exporter, provider, instrumentor
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def sdk(async_=False, handler=None):
    handler = handler or (lambda request: httpx.Response(200, json=completion()))
    transport = httpx.MockTransport(handler)
    if async_:
        return openai.AsyncOpenAI(api_key=FAKE_KEY, base_url="http://offline.invalid/v1",
                                 http_client=httpx.AsyncClient(transport=transport))
    return openai.OpenAI(api_key=FAKE_KEY, base_url="http://offline.invalid/v1",
                         http_client=httpx.Client(transport=transport))


@pytest.mark.parametrize("route", ["patch", "patch_create", "create", "create_with_completion"])
def test_sync_request_routes(traces, route):
    exporter, _, _ = traces
    with sdk() as raw:
        if route == "patch":
            create = instructor.patch(raw, mode=instructor.Mode.JSON).chat.completions.create
        elif route == "patch_create":
            create = instructor.patch(create=raw.chat.completions.create, mode=instructor.Mode.JSON)
        else:
            create = getattr(instructor.from_openai(raw, mode=instructor.Mode.JSON), route)
        assert exporter.get_finished_spans() == ()  # construction is not an extraction
        result = create(**PARAMS)
        if route == "create_with_completion":
            result, raw_response = result
            assert raw_response.id == "offline"
        assert result.name == "Ada"
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.span.kind"] == "CHAIN"
    assert json.loads(span.attributes["output.value"]) == {"name": "Ada", "age": 37}
    assert FAKE_KEY not in str(span.attributes)
    assert span.status.status_code == trace.StatusCode.OK


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["patch", "patch_create", "create", "create_with_completion"])
async def test_async_request_routes(traces, route):
    exporter, _, _ = traces
    async with sdk(async_=True) as raw:
        if route == "patch":
            create = instructor.patch(raw, mode=instructor.Mode.JSON).chat.completions.create
        elif route == "patch_create":
            create = instructor.patch(create=raw.chat.completions.create, mode=instructor.Mode.JSON)
        else:
            create = getattr(instructor.from_openai(raw, mode=instructor.Mode.JSON), route)
        assert exporter.get_finished_spans() == ()
        result = await create(**PARAMS)
        if route == "create_with_completion":
            result, _ = result
        assert result.age == 37
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.span.kind"] == "CHAIN"
    assert json.loads(span.attributes["output.value"]) == {"name": "Ada", "age": 37}


@pytest.mark.skipif(not CURRENT, reason="Official hooks introduced after Instructor 1.0")
def test_retry_hooks_are_per_call_and_preserve_user_handlers(traces):
    exporter, _, _ = traces
    attempts = []
    def respond(request):
        attempts.append(1)
        return httpx.Response(200, json=completion("invalid" if len(attempts) == 1 else 37))
    with sdk(handler=respond) as raw:
        client = instructor.from_openai(raw, mode=instructor.Mode.JSON)
        seen = []
        client.on("completion:response", lambda response: seen.append(response.id))
        before = {k: list(v) for k, v in client.hooks._handlers.items()}
        client.create(**PARAMS, max_retries=2)
        client.create(**PARAMS)
        assert dict(client.hooks._handlers) == before
    first, second = exporter.get_finished_spans()
    assert seen == ["offline"] * 3
    assert first.attributes["instructor.attempts"] == 2
    assert first.attributes["instructor.validation_errors"] == 1
    assert first.attributes["gen_ai.usage.input_tokens"] == 10
    assert first.attributes["gen_ai.usage.output_tokens"] == 6
    assert second.attributes["instructor.attempts"] == 1
    assert second.attributes["gen_ai.usage.input_tokens"] == 5


def test_wire_privacy_and_single_encoded_model(traces):
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    exporter, provider, instrumentor = traces
    with sdk() as raw:
        patched = instructor.patch(raw, mode=instructor.Mode.JSON)
        assert not exporter.get_finished_spans()
        patched.chat.completions.create(**PARAMS, extra_headers={"x-secret": FAKE_KEY})
    request = encode_spans(exporter.get_finished_spans())
    assert FAKE_KEY.encode() not in request.SerializeToString()
    span, = request.resource_spans[0].scope_spans[0].spans
    attrs = {a.key: a.value.string_value for a in span.attributes}
    assert json.loads(attrs["output.value"]) == {"name": "Ada", "age": 37}
    exporter.clear()
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    with sdk() as raw:
        instructor.from_openai(raw, mode=instructor.Mode.JSON).create(**PARAMS)
    wire = encode_spans(exporter.get_finished_spans()).SerializeToString()
    assert b"Ada" not in wire


def streaming_response(iterable=False):
    text = json.dumps({"tasks": [{"name": "Ada", "age": 37}, {"name": "Grace", "age": 40}]}
                      if iterable else {"name": "Ada", "age": 37})
    events = []
    for part in text:
        chunk = {"id": "offline", "object": "chat.completion.chunk", "created": 1,
                 "model": "offline-model", "choices": [{"index": 0,
                 "delta": {"content": part}, "finish_reason": None}]}
        events.append("data: " + json.dumps(chunk) + "\n\n")
    return httpx.Response(200, content=("".join(events) + "data: [DONE]\n\n").encode(),
                          headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("method", ["create_partial", "create_iterable"])
def test_real_sync_stream_routes(traces, method):
    exporter, _, _ = traces
    with sdk(handler=lambda request: streaming_response(method == "create_iterable")) as raw:
        client = instructor.from_openai(raw, mode=instructor.Mode.JSON)
        if method == "create_partial" and version("instructor") == "1.0.0":
            # The floor SDK calls create_model(__model_name=...), incompatible
            # with Pydantic >=2.9 required by our core. Preserve its exception.
            _, provider, instrumentor = traces
            instrumentor.uninstrument()
            with pytest.raises(TypeError, match="model_name"):
                client.create_partial(**PARAMS)
            assert not exporter.get_finished_spans()
            instrumentor.instrument(tracer_provider=provider)
            with pytest.raises(TypeError, match="model_name"):
                client.create_partial(**PARAMS)
            span, = exporter.get_finished_spans()
            assert span.status.status_code == trace.StatusCode.ERROR
            return
        stream = getattr(client, method)(**PARAMS)
        # Instructor 1.17 materializes sync partials as a ListResponse itself.
        # Never make an eager SDK result lazy (or consume a lazy one eagerly).
        eager = isinstance(stream, list)
        assert bool(exporter.get_finished_spans()) == eager
        chunks = list(stream)
    assert chunks[-1].name == ("Grace" if method == "create_iterable" else "Ada")
    span, = exporter.get_finished_spans()
    assert span.name == "instructor." + method
    if not eager:
        assert span.attributes["instructor.stream.chunks"] == len(chunks)
        assert json.loads(span.attributes["output.value"])["name"] == chunks[-1].name
    else:
        assert json.loads(span.attributes["output.value"])[-1]["name"] == chunks[-1].name


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["create_partial", "create_iterable"])
async def test_real_async_stream_routes(traces, method):
    exporter, _, _ = traces
    async with sdk(async_=True, handler=lambda request: streaming_response(method == "create_iterable")) as raw:
        client = instructor.from_openai(raw, mode=instructor.Mode.JSON)
        if method == "create_partial" and version("instructor") == "1.0.0":
            _, provider, instrumentor = traces
            instrumentor.uninstrument()
            with pytest.raises(TypeError, match="model_name"):
                [item async for item in client.create_partial(**PARAMS)]
            assert not exporter.get_finished_spans()
            instrumentor.instrument(tracer_provider=provider)
            with pytest.raises(TypeError, match="model_name"):
                [item async for item in client.create_partial(**PARAMS)]
            span, = exporter.get_finished_spans()
            assert span.status.status_code == trace.StatusCode.ERROR
            return
        stream = getattr(client, method)(**PARAMS)
        assert not exporter.get_finished_spans()
        chunks = [item async for item in stream]
    assert chunks[-1].name == ("Grace" if method == "create_iterable" else "Ada")
    span, = exporter.get_finished_spans()
    assert span.name == "instructor." + method
    assert span.attributes["instructor.stream.chunks"] == len(chunks)


@pytest.mark.skipif(not CURRENT, reason="v2 factories/cache introduced after Instructor 1.0")
def test_generated_v2_factory_and_cache_hits(traces):
    from instructor.cache import AutoCache
    from instructor.v2.core.patch import patch_v2
    from instructor.v2.core.providers import Provider
    exporter, _, _ = traces
    calls = []
    def respond(request):
        calls.append(1)
        return httpx.Response(200, json=completion())
    with sdk(handler=respond) as raw:
        create = patch_v2(raw.chat.completions.create, Provider.OPENAI, instructor.Mode.JSON)
        cache = AutoCache()
        for _ in range(2):
            assert create(**PARAMS, cache=cache).name == "Ada"
    first, cached = exporter.get_finished_spans()
    assert len(calls) == 1
    assert first.attributes["instructor.attempts"] == 1
    assert "instructor.attempts" not in cached.attributes
    assert "gen_ai.usage.input_tokens" not in cached.attributes  # no invented cache usage
    assert json.loads(cached.attributes["output.value"])["name"] == "Ada"


@pytest.mark.skipif(not CURRENT, reason="Official hooks introduced after Instructor 1.0")
@pytest.mark.asyncio
async def test_concurrent_calls_keep_hooks_and_parentage_isolated(traces):
    exporter, provider, _ = traces
    ready = asyncio.Event()
    calls = []
    async def respond(request):
        calls.append(1)
        if len(calls) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        with provider.get_tracer("provider").start_as_current_span("provider.completion"):
            return httpx.Response(200, json=completion())
    async with sdk(async_=True, handler=respond) as raw:
        client = instructor.from_openai(raw, mode=instructor.Mode.JSON)
        from instructor.core.hooks import Hooks
        hooks = Hooks()
        seen = []
        hooks.on("completion:response", lambda response: seen.append(response.id))
        with provider.get_tracer("app").start_as_current_span("parent") as parent:
            await asyncio.gather(*(client.create(**PARAMS, hooks=hooks) for _ in range(2)))
        assert len(seen) == 2
        assert sum(map(len, hooks._handlers.values())) == 1
        assert sum(map(len, client.hooks._handlers.values())) == 0
    requests = [s for s in exporter.get_finished_spans() if s.name == "instructor.create"]
    providers = [s for s in exporter.get_finished_spans() if s.name == "provider.completion"]
    assert len(requests) == len(providers) == 2
    assert {s.parent.span_id for s in providers} == {s.context.span_id for s in requests}
    assert all(s.parent.span_id == parent.get_span_context().span_id for s in requests)
    assert all(s.attributes["instructor.attempts"] == 1 for s in requests)
    assert all(s.attributes["gen_ai.usage.input_tokens"] == 5 for s in requests)


def test_positional_public_arguments(traces):
    exporter, _, _ = traces
    with sdk() as raw:
        client = instructor.from_openai(raw, mode=instructor.Mode.JSON)
        assert client.create(Person, PARAMS["messages"], model=PARAMS["model"]).name == "Ada"
    span, = exporter.get_finished_spans()
    assert json.loads(span.attributes["input.value"]) == [{"role": "user", "content": "Ada is 37"}]


@pytest.mark.skipif(not CURRENT, reason="Per-call hooks require a recent SDK")
def test_positional_generated_hooks(traces):
    from instructor.core.hooks import Hooks
    exporter, _, _ = traces
    seen = []
    hooks = Hooks()
    hooks.on("completion:response", lambda response: seen.append(response.id))
    with sdk() as raw:
        create = instructor.patch(create=raw.chat.completions.create, mode=instructor.Mode.JSON)
        result = create(Person, None, 1, True, hooks, model=PARAMS["model"], messages=PARAMS["messages"])
        assert result.name == "Ada"
    span, = exporter.get_finished_spans()
    assert span.attributes["instructor.attempts"] == 1
    assert seen == ["offline"]
    assert sum(map(len, hooks._handlers.values())) == 1


@pytest.mark.skipif(version("instructor") == "1.0.0", reason="Hooks unavailable at floor")
def test_legacy_per_call_hooks_without_mutating_registrations(traces):
    try:
        from instructor.core.hooks import Hooks
    except ImportError:
        from instructor.hooks import Hooks
    exporter, _, _ = traces
    hooks = Hooks()
    seen = []
    hooks.on("completion:response", lambda response: seen.append(response.id))
    with sdk() as raw:
        create = instructor.patch(create=raw.chat.completions.create, mode=instructor.Mode.JSON)
        assert create(**PARAMS, hooks=hooks).name == "Ada"
    span, = exporter.get_finished_spans()
    assert span.attributes["instructor.attempts"] == 1
    assert seen == ["offline"]
    assert sum(map(len, hooks._handlers.values())) == 1


def test_generated_callables_are_disabled_on_uninstrument(traces):
    exporter, _, instrumentor = traces
    with sdk() as raw:
        create = instructor.patch(create=raw.chat.completions.create, mode=instructor.Mode.JSON)
        exporter.clear()
        instrumentor.uninstrument()
        assert create(**PARAMS).name == "Ada"
        assert not exporter.get_finished_spans()
