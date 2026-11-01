"""TH-8320 review round 5.

Default (no read after a stream):

* a text chunk that starts like a URL (``https://``, ``http://``, ``data:``)
  stays in the output, in order, for streams and run iterators, and a text
  stream reports no file count;
* a KeyboardInterrupt while the span is completed at ``done`` still ends
  (and exports) the span;
* ``send()`` / ``throw()`` and ``asend()`` / ``athrow()`` step a stream as
  ``next()`` / ``__anext__()`` do: one ``replicate.stream`` span, no stray
  create span.

Opt-in read (``stream_reload_seconds`` > 0):

* while httpx's or httpcore's send path is replaced on the class (respx,
  pytest-httpx, VCR.py, ``mock.patch``), no read is made
  (``replicate.stream.skip_reason`` = ``patched_http``), checked when the
  stream completes and again before the request; the app's own scripted
  responses are left to the app;
* only replicate's own ``RetryTransport`` is looked through, not a subclass;
  a transport or pool changed on the instance, and a client with a proxy,
  get no read (``custom_transport``).

Everything runs against the loopback ``FakeReplicate``; no live call is made.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from unittest import mock

import httpx
import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

import httpcore  # noqa: E402
import replicate  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402
from replicate.client import RetryTransport  # noqa: E402
from traceai_replicate import _attributes, _stream_read, _wrappers  # noqa: E402

from _support import (  # noqa: E402
    API_TOKEN,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    STREAM_MODEL,
    STREAM_TEXT,
    STREAM_URL_TEXT_MODEL,
    URL_TEXT,
    URL_TEXT_MODEL,
    URL_TEXT_VERSION,
    READ_ON,
    FakeReplicate,
    attrs,
    default_client,
    instrumented,
    make_client,
    own_sends,
)

READ = "/v1/predictions/pred0001"
STATUS = "replicate.prediction.status"
FINAL_KEYS = (STATUS, "replicate.metrics.predict_time", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")


def _reads(fake):
    return [path for path in fake.paths("GET") if path.startswith("/v1/predictions/")]


def _final(values):
    assert values[STATUS] == "succeeded", values.get("replicate.stream.read")
    assert values["gen_ai.usage.input_tokens"] == INPUT_TOKENS
    assert values["gen_ai.usage.output_tokens"] == OUTPUT_TOKENS


def _no_final(values):
    for key in FINAL_KEYS:
        assert key not in values, key


def _skipped(values, reason):
    assert (values.get("replicate.stream.read"), values.get("replicate.stream.skip_reason")) == ("skipped", reason)


def _stream_spans(traced):
    return [span for span in traced.replicate_spans() if span.name == "replicate.stream"]


# -- default: URL-like text chunks are text --------------------------------------------------------------


def _url_text_span(span):
    values = attrs(span)
    assert span.status.status_code is StatusCode.OK
    assert values["output.value"] == URL_TEXT
    assert values["replicate.output.type"] == "text"
    assert values["gen_ai.span.kind"] == "LLM"
    assert "replicate.stream.file_count" not in values
    return values


def test_a_stream_keeps_text_chunks_that_start_like_urls_in_its_output():
    with FakeReplicate() as fake, instrumented() as traced:
        list(default_client(fake).stream(STREAM_URL_TEXT_MODEL, input={}))
        span = traced.one()

    _url_text_span(span)


def test_an_async_stream_keeps_text_chunks_that_start_like_urls_in_its_output():
    async def consume(client):
        return [event async for event in await client.async_stream(STREAM_URL_TEXT_MODEL, input={})]

    with FakeReplicate() as fake, instrumented() as traced:
        asyncio.run(consume(default_client(fake)))
        span = traced.one()

    _url_text_span(span)


def test_a_run_iterator_keeps_text_chunks_that_start_like_urls_in_its_output():
    with FakeReplicate() as fake, instrumented() as traced:
        output = make_client(fake).run("{0}:{1}".format(URL_TEXT_MODEL, URL_TEXT_VERSION), input={})
        assert not isinstance(output, (list, str))  # an iterator
        chunks = [getattr(chunk, "url", chunk) for chunk in output]
        span = traced.one()

    assert "".join(chunks) == URL_TEXT
    assert span.name == "replicate.run"
    _url_text_span(span)


class FileOutput:
    """Stands in for replicate's FileOutput (matched by name, read only through ``.url``)."""

    def __init__(self, url):
        self.url = url


def test_url_like_chunks_and_file_outputs_join_text_in_order_and_files_stay_files():
    text = _attributes.output_attributes(["See ", "https://", "example.com", " and ", FileOutput("data:"), "x"])
    assert (text["replicate.output.type"], text["output.value"]) == ("text", "See https://example.com and data:x")

    files = _attributes.output_attributes(["https://a.example.invalid/1.png", FileOutput("https://b.example.invalid/2")])
    assert files["replicate.output.type"] == "list"
    assert json.loads(files["output.value"]) == ["https://a.example.invalid/1.png", "https://b.example.invalid/2"]

    stream = _attributes.OutputCollector(count_files=True)
    for item in ("https://a.example.invalid/1.png", FileOutput("https://b.example.invalid/2")):
        stream.add(item)
    assert stream.attributes() == {"replicate.output.type": "list", "replicate.stream.file_count": 2}

    mixed = _attributes.OutputCollector(count_files=True)
    for item in ("See ", FileOutput("https://"), "example.com"):
        mixed.add(item)
    assert mixed.attributes()["output.value"] == "See https://example.com"
    assert "replicate.stream.file_count" not in mixed.attributes()


# -- default: a KeyboardInterrupt at done still ends the span ----------------------------------------------


def _interrupted_at_done(traced, client):
    events = client.stream(STREAM_MODEL, input={})
    with pytest.raises(KeyboardInterrupt):
        for _ in events:
            pass
    events.close()  # completed at done: not a cancellation
    del events
    traced.provider.force_flush()


@pytest.mark.parametrize("read", ["off", "on-skipped"])
def test_a_keyboard_interrupt_while_the_span_is_written_at_done_still_ends_it(read, monkeypatch):
    """The span ends on the caller's thread at done: with the read off, or
    with it on and skipped (here: a custom transport)."""
    original = _wrappers._Call._write
    raised = []

    def interrupted(self, *args, **kwargs):
        if self.stream and not raised:
            raised.append(True)
            raise KeyboardInterrupt
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_wrappers._Call, "_write", interrupted)
    with FakeReplicate() as fake, instrumented(**(READ_ON if read == "on-skipped" else {})) as traced:
        client = make_client(fake) if read == "on-skipped" else default_client(fake)
        _interrupted_at_done(traced, client)
        spans = _stream_spans(traced)

    assert raised == [True]
    assert len(spans) == 1  # ended and exported, without its attributes
    assert spans[0].end_time is not None


@pytest.mark.parametrize("read", ["off", "on"])
def test_a_keyboard_interrupt_while_dones_data_is_read_still_ends_the_span(read, monkeypatch):
    def interrupted(done):
        raise KeyboardInterrupt

    monkeypatch.setattr(_stream_read, "stream_attributes", interrupted)
    with FakeReplicate() as fake, instrumented(**(READ_ON if read == "on" else {})) as traced:
        _interrupted_at_done(traced, default_client(fake))
        spans = _stream_spans(traced)
        reads = _reads(fake)

    assert len(spans) == 1
    values = attrs(spans[0])
    assert spans[0].status.status_code is StatusCode.OK
    assert values["output.value"] == STREAM_TEXT
    assert "replicate.cancelled" not in values
    assert reads == []
    if read == "on":
        _skipped(values, "interrupted")
    else:
        assert "replicate.stream.read" not in values


# -- default: send() / asend() step the stream ------------------------------------------------------------


def test_a_stream_driven_with_send_is_one_stream_span():
    with FakeReplicate() as fake, instrumented() as traced:
        events = default_client(fake).stream(STREAM_MODEL, input={})
        seen = []
        with pytest.raises(StopIteration):
            while True:
                seen.append(events.send(None))
        spans = traced.replicate_spans()

    assert [span.name for span in spans] == ["replicate.stream"]
    values = attrs(spans[0])
    assert spans[0].status.status_code is StatusCode.OK
    assert values["output.value"] == STREAM_TEXT


def test_an_async_stream_driven_with_asend_is_one_stream_span():
    async def consume(client):
        events = await client.async_stream(STREAM_MODEL, input={})
        seen = []
        try:
            while True:
                seen.append(await events.asend(None))
        except StopAsyncIteration:
            return seen

    with FakeReplicate() as fake, instrumented() as traced:
        asyncio.run(consume(default_client(fake)))
        spans = traced.replicate_spans()

    assert [span.name for span in spans] == ["replicate.stream"]
    assert attrs(spans[0])["output.value"] == STREAM_TEXT


def test_throw_into_a_stream_ends_its_span_with_that_error():
    with FakeReplicate() as fake, instrumented() as traced:
        events = default_client(fake).stream(STREAM_MODEL, input={})
        next(events)
        with pytest.raises(RuntimeError):
            events.throw(RuntimeError("caller's error"))
        spans = traced.replicate_spans()

    assert [span.name for span in spans] == ["replicate.stream"]
    assert spans[0].status.status_code is StatusCode.ERROR
    assert [event.attributes["exception.type"] for event in spans[0].events] == ["RuntimeError"]


def test_athrow_into_an_async_stream_ends_its_span_with_that_error():
    async def consume(client):
        events = await client.async_stream(STREAM_MODEL, input={})
        await events.__anext__()
        with pytest.raises(RuntimeError):
            await events.athrow(RuntimeError("caller's error"))

    with FakeReplicate() as fake, instrumented() as traced:
        asyncio.run(consume(default_client(fake)))
        spans = traced.replicate_spans()

    assert [span.name for span in spans] == ["replicate.stream"]
    assert spans[0].status.status_code is StatusCode.ERROR


# -- opt-in: no read while an HTTP mock patches httpx or httpcore ------------------------------------------


def _path(request):
    """The path of an httpx or httpcore request, or of a URL."""
    url = getattr(request, "url", request)
    if isinstance(url, httpx.URL):
        return url.path
    target = getattr(url, "target", b"")
    return target.decode("ascii", "replace").split("?")[0]


def _pass_through(original, calls):
    """A replacement defined here, as respx's and pytest-httpx's are defined in theirs."""
    if asyncio.iscoroutinefunction(original):

        async def replacement(self, request, *args, **kwargs):
            calls.append(_path(request))
            return await original(self, request, *args, **kwargs)

    else:

        def replacement(self, request, *args, **kwargs):
            calls.append(_path(request))
            return original(self, request, *args, **kwargs)

    return replacement


def _wraps(original, calls):
    """A ``functools.wraps`` copy, as VCR.py patches httpx: same name, module and ``__wrapped__``."""
    return functools.wraps(original)(_pass_through(original, calls))


PATCHES = {
    # respx's default mocker: httpcore's pools and connections
    "httpcore-pools": [(httpcore.AsyncConnectionPool, "handle_async_request"), (httpcore.ConnectionPool, "handle_request")],
    # pytest-httpx: httpx's transports
    "httpx-transports": [(httpx.AsyncHTTPTransport, "handle_async_request"), (httpx.HTTPTransport, "handle_request")],
    # respx's httpx mocker
    "transport-for-url": [(httpx.AsyncClient, "_transport_for_url")],
    "async-client-send": [(httpx.AsyncClient, "send")],
}


@pytest.mark.parametrize("style", ["plain", "wraps", "mock"])
@pytest.mark.parametrize("target", sorted(PATCHES))
def test_with_the_read_on_an_http_mock_on_the_class_gets_no_read(target, style, monkeypatch):
    calls = []
    for owner, name in PATCHES[target]:
        original = owner.__dict__[name]
        if style == "plain":
            replacement = _pass_through(original, calls)
        elif style == "wraps":
            replacement = _wraps(original, calls)
        else:
            replacement = mock.create_autospec(original, side_effect=original)
        monkeypatch.setattr(owner, name, replacement)

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced, own_sends() as sent:
        events = list(default_client(fake).stream(STREAM_MODEL, input={}))
        traced.settle()  # a read, if one were made, would end the span
        span = traced.one()
        reads = _reads(fake)

    assert len(events) > 1
    assert reads == [] and sent == []
    assert not [path for path in calls if path == READ]
    values = attrs(span)
    _no_final(values)
    _skipped(values, "patched_http")


def test_with_the_read_on_reads_resume_once_the_mock_is_removed():
    calls = []
    with mock.patch.object(
        httpcore.AsyncConnectionPool,
        "handle_async_request",
        _pass_through(httpcore.AsyncConnectionPool.handle_async_request, calls),
    ):
        assert _stream_read.send_path_patched()
    assert not _stream_read.send_path_patched()

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        traced.settle()
        span = traced.one()

    _final(attrs(span))


def test_with_the_read_on_opentelemetry_httpx_instrumentation_is_not_a_mock():
    httpx_instrumentation = pytest.importorskip(
        "opentelemetry.instrumentation.httpx", reason="opentelemetry-instrumentation-httpx is optional"
    )
    instrumentor = httpx_instrumentation.HTTPXClientInstrumentor()
    instrumentor.instrument()
    try:
        assert not _stream_read.send_path_patched()
    finally:
        instrumentor.uninstrument()


def test_with_the_read_on_a_mock_started_after_done_stops_the_read_before_it_is_sent(monkeypatch):
    answers = iter([False])  # not patched when the stream completes; patched by the time the read is sent
    monkeypatch.setattr(_stream_read, "send_path_patched", lambda: next(answers, True))
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced, own_sends() as sent:
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        traced.settle()
        span = traced.one()
        reads = _reads(fake)

    assert sent == [("GET", READ)]  # handed to the package's send, which stopped it
    assert reads == []
    values = attrs(span)
    _no_final(values)
    _skipped(values, "patched_http")


SCRIPTED_BASE = "https://api.replicate.com"
SCRIPTED_CREATED = {
    "id": "abc123",
    "model": "acme/llm",
    "version": "v1",
    "status": "starting",
    "input": {},
    "output": None,
    "logs": "",
    "error": None,
    "metrics": {},
    "created_at": "2026-10-20T00:00:00Z",
    "urls": {
        "get": SCRIPTED_BASE + "/v1/predictions/abc123",
        "cancel": SCRIPTED_BASE + "/v1/predictions/abc123/cancel",
        "stream": SCRIPTED_BASE + "/stream/abc123",
    },
}
SCRIPTED_FINAL = dict(SCRIPTED_CREATED, status="succeeded", output=["hello"], logs="APP-OWN-SCRIPTED-RESPONSE")
SCRIPTED_SSE = b"event: output\nid: 1\ndata: hello\n\nevent: done\nid: 2\ndata: {}\n\n"


@pytest.mark.parametrize("mocker", ["httpcore", "httpx"])
def test_with_the_read_on_respx_keeps_the_apps_scripted_response(mocker):
    respx = pytest.importorskip("respx", reason="respx is a dev dependency")
    with instrumented(stream_reload_seconds=2.0) as traced:
        with respx.mock(assert_all_called=False, using=mocker) as router:
            router.post(SCRIPTED_BASE + "/v1/models/acme/llm/predictions").mock(
                return_value=httpx.Response(201, json=SCRIPTED_CREATED)
            )
            router.get(SCRIPTED_BASE + "/stream/abc123").mock(
                return_value=httpx.Response(200, content=SCRIPTED_SSE, headers={"content-type": "text/event-stream"})
            )
            # The app scripts one answer for its own predictions.get().
            get_route = router.get(SCRIPTED_BASE + "/v1/predictions/abc123").mock(
                side_effect=[httpx.Response(200, json=SCRIPTED_FINAL)]
            )
            client = replicate.Client(api_token=API_TOKEN)
            events = [str(event) for event in client.stream("acme/llm", input={})]
            traced.settle()  # a read, if one were made, would end the span
            span = traced.one()
            try:
                prediction = client.predictions.get("abc123")
            except Exception as error:  # the app's one scripted answer was used up
                prediction = error
            calls = get_route.call_count

    assert "hello" in events
    assert not isinstance(prediction, Exception), repr(prediction)
    assert (prediction.status, prediction.logs) == ("succeeded", "APP-OWN-SCRIPTED-RESPONSE")
    assert calls == 1  # the app's own get, nothing else
    values = attrs(span)
    _no_final(values)
    _skipped(values, "patched_http")


# -- opt-in: only replicate's own RetryTransport is looked through ------------------------------------------


class _SigningRetry(RetryTransport):
    """A caller's subclass of replicate's RetryTransport (signing, auth, logging)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def handle_request(self, request):
        request.headers["X-Caller-Signed"] = "yes"
        self.calls.append((request.method, request.url.path))
        return super().handle_request(request)


CALLER_TRANSPORTS = {
    "retry-subclass-around-plain": lambda: _SigningRetry(httpx.HTTPTransport()),
    "retry-around-retry-subclass": lambda: RetryTransport(_SigningRetry(httpx.HTTPTransport())),
}


@pytest.mark.parametrize("kind", sorted(CALLER_TRANSPORTS))
def test_with_the_read_on_a_subclass_of_replicates_retry_transport_gets_no_read(kind):
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced, own_sends() as sent:
        client = default_client(fake, transport=CALLER_TRANSPORTS[kind]())
        events = list(client.stream(STREAM_MODEL, input={}))
        traced.settle()
        span = traced.one()
        reads = _reads(fake)
        signed = fake.request_headers("GET", "/stream/pred0001").get("x-caller-signed")

    assert len(events) > 1 and signed == "yes"  # the caller's code ran for its own requests
    assert reads == [] and sent == []
    values = attrs(span)
    _no_final(values)
    _skipped(values, "custom_transport")


@pytest.mark.parametrize("where", ["transport", "pool", "default-inner-transport"])
def test_with_the_read_on_a_transport_changed_on_the_instance_gets_no_read(where):
    calls = []
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced, own_sends() as sent:
        if where == "default-inner-transport":
            client = default_client(fake)
            target = client._client._transport._wrapped_transport  # replicate built it
        else:
            transport = httpx.HTTPTransport()
            client = default_client(fake, transport=transport)
            target = transport if where == "transport" else transport._pool
        original = target.handle_request

        def double(request):
            calls.append(_path(request))
            return original(request)

        with mock.patch.object(target, "handle_request", side_effect=double):
            events = list(client.stream(STREAM_MODEL, input={}))
            traced.settle()
            span = traced.one()
        reads = _reads(fake)

    assert len(events) > 1 and calls  # the double served the app
    assert READ not in calls
    assert reads == [] and sent == []
    values = attrs(span)
    _no_final(values)
    _skipped(values, "custom_transport")


def test_with_the_read_on_a_client_with_a_proxy_gets_no_read():
    for client in (
        replicate.Client(api_token=API_TOKEN, proxy="http://127.0.0.1:9"),
        replicate.Client(api_token=API_TOKEN, transport=httpx.HTTPTransport(proxy="http://127.0.0.1:9")),
    ):
        for http in (client._client, client._async_client):  # built lazily; nothing is sent
            url = _stream_read._read_url(http, "pred0001")
            assert _stream_read._read_routing(client, http, url) is None
    default = replicate.Client(api_token=API_TOKEN)
    url = _stream_read._read_url(default._client, "pred0001")
    assert _stream_read._read_routing(default, default._client, url)[0] == "client"
    assert _stream_read._read_routing(default, default._async_client, url)[0] == "client"
