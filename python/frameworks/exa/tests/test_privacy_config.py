"""TraceConfig and context: hidden inputs, FITracer attributes, and safe error text."""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")

from _support import (  # noqa: E402
    ECHO_PADDING,
    ECHO_QUERY,
    EXA_KEY,
    FAIL_QUERY,
    RESULT_TITLE,
    FakeExa,
    attrs,
    instrumented,
)
from exa_py import AsyncExa, Exa  # noqa: E402
from fi_instrumentation import TraceConfig, using_session  # noqa: E402

HIDDEN = "__REDACTED__"
PRIVATE = "private question"


@pytest.fixture()
def fake():
    with FakeExa() as server:
        yield server


def _client(fake: FakeExa) -> Exa:
    return Exa(api_key=EXA_KEY, base_url=fake.origin)


def _exception_event(span):
    (event,) = [event for event in span.events if event.name == "exception"]
    return dict(event.attributes)


# --- hide_inputs ------------------------------------------------------------


def test_hide_inputs_config_hides_the_query_and_keeps_the_count(fake):
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        _client(fake).search(PRIVATE)

    span_attributes = attrs(traced.one())
    assert span_attributes["input.value"] == HIDDEN
    assert "fi.retrieval.query" not in span_attributes
    assert span_attributes["fi.retrieval.document_count"] == 2
    assert PRIVATE not in traced.wire()
    # Hiding is on the span only; the vendor still got the caller's query.
    assert fake.calls[0][2]["query"] == PRIVATE


def test_fi_hide_inputs_environment_variable_is_honoured(fake, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    with instrumented() as traced:
        _client(fake).answer(PRIVATE)

    span_attributes = attrs(traced.one())
    assert span_attributes["input.value"] == HIDDEN
    assert "fi.retrieval.query" not in span_attributes
    assert PRIVATE not in traced.wire()


def test_hide_inputs_applies_to_streams(fake):
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        for _ in _client(fake).stream_search(PRIVATE):
            pass

    span_attributes = attrs(traced.one())
    assert span_attributes["input.value"] == HIDDEN
    assert "fi.retrieval.query" not in span_attributes
    assert PRIVATE not in traced.wire()


def test_hide_inputs_wins_over_capture_urls(fake):
    urls = ["https://example.com/a?token=t-1", "https://example.com/b"]
    with instrumented(capture_urls=True, config=TraceConfig(hide_inputs=True)) as traced:
        _client(fake).get_contents(urls)

    span_attributes = attrs(traced.one())
    assert span_attributes["fi.retrieval.url_count"] == 2
    assert "fi.retrieval.urls" not in span_attributes
    assert "example.com" not in traced.wire()


def test_config_must_be_a_trace_config():
    from traceai_exa import ExaInstrumentor

    instrumentor = ExaInstrumentor()
    try:
        with pytest.raises(TypeError, match="TraceConfig"):
            instrumentor.instrument(
                tracer_provider=TracerProvider(), config={"hide_inputs": True}
            )
        # Nothing was wrapped, so a later instrument() works normally.
        assert not getattr(Exa.search, "__wrapped__", None)
    finally:
        instrumentor.uninstrument()


# --- FITracer context attributes -------------------------------------------


def test_context_attributes_reach_the_exa_span(fake):
    with instrumented() as traced:
        with using_session("session-1"):
            _client(fake).search("context")

    assert attrs(traced.one())["session.id"] == "session-1"


# --- error text --------------------------------------------------------------


def _echo_error_cases():
    def sync_search(client_fake, query):
        _client(client_fake).search(query)

    def sync_stream(client_fake, query):
        for _ in _client(client_fake).stream_search(query):
            pass

    def async_search(client_fake, query):
        async def call() -> None:
            client = AsyncExa(api_key=EXA_KEY, api_base=client_fake.origin)
            try:
                await client.search(query)
            finally:
                await client.client.aclose()

        asyncio.run(call())

    return [sync_search, sync_stream, async_search]


@pytest.mark.parametrize("call", _echo_error_cases(), ids=["search", "stream", "async"])
def test_error_text_has_the_key_redacted_and_is_capped(fake, call):
    query = "{0} find {1}".format(ECHO_QUERY, EXA_KEY)
    with instrumented() as traced:
        with pytest.raises(ValueError) as caught:
            call(fake, query)

    # The caller's exception is unchanged: it still carries the server's text.
    assert EXA_KEY in str(caught.value)
    assert ECHO_PADDING in str(caught.value)

    span = traced.one()
    assert EXA_KEY not in traced.wire()
    description = span.status.description
    assert description.startswith("ValueError: Request failed with status code 400")
    assert "[redacted]" in description
    assert len(description.encode("utf-8")) <= len("ValueError: ") + 1024

    event = _exception_event(span)
    assert event["exception.type"] == "ValueError"
    assert "[redacted]" in event["exception.message"]
    assert len(event["exception.message"].encode("utf-8")) <= 1024
    assert event["exception.stacktrace"].startswith("Traceback (most recent call last):")
    assert EXA_KEY not in event["exception.stacktrace"]
    assert ECHO_PADDING not in event["exception.stacktrace"]


@pytest.mark.parametrize("call", _echo_error_cases(), ids=["search", "stream", "async"])
def test_hidden_inputs_also_leave_error_text(fake, call):
    query = "{0} {1}".format(ECHO_QUERY, PRIVATE)
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        with pytest.raises(ValueError) as caught:
            call(fake, query)

    assert PRIVATE in str(caught.value)
    span = traced.one()
    assert PRIVATE not in traced.wire()
    assert HIDDEN in span.status.description
    assert HIDDEN in _exception_event(span)["exception.message"]


def test_short_error_text_is_kept_whole(fake):
    with instrumented() as traced:
        with pytest.raises(ValueError) as caught:
            _client(fake).search(FAIL_QUERY)

    span = traced.one()
    assert span.status.description == "ValueError: {0}".format(caught.value)
    assert _exception_event(span)["exception.message"] == str(caught.value)


def test_context_is_restored_after_a_vendor_error(fake):
    with instrumented():
        with pytest.raises(ValueError):
            _client(fake).search(FAIL_QUERY)
        assert not trace_api.get_current_span().get_span_context().is_valid


# --- streams that fail mid-iteration ----------------------------------------


class _Chunk:
    citations = None


class _BrokenStream:
    """Yields one chunk, then fails as a dropped connection would."""

    def __init__(self) -> None:
        self.closed = False

    def __iter__(self):
        yield _Chunk()
        raise ConnectionError("connection dropped")

    async def _agen(self):
        yield _Chunk()
        raise ConnectionError("connection dropped")

    def __aiter__(self):
        return self._agen()

    def close(self) -> None:
        self.closed = True


def _tracer(traced):
    from fi_instrumentation import FITracer

    return FITracer(traced.provider.get_tracer("test"), config=TraceConfig())


def test_sync_stream_failing_mid_iteration_is_an_error_not_a_cancel():
    from traceai_exa._wrappers import StreamWrapper

    with instrumented() as traced:
        wrapper = StreamWrapper(_tracer(traced), "exa.search")
        stream = wrapper(lambda *a, **k: _BrokenStream(), None, ("q",), {})
        next(stream)
        with pytest.raises(ConnectionError):
            next(stream)

    span = traced.one()
    assert span.status.description == "ConnectionError: connection dropped"
    assert "exa.cancelled" not in attrs(span)
    assert "fi.retrieval.document_count" not in attrs(span)


def test_async_stream_failing_mid_iteration_is_an_error_not_a_cancel():
    from traceai_exa._wrappers import AsyncStreamWrapper

    async def run(traced) -> None:
        wrapper = AsyncStreamWrapper(_tracer(traced), "exa.search")
        stream = await wrapper(lambda *a, **k: _BrokenStream(), None, ("q",), {})
        await stream.__anext__()
        with pytest.raises(ConnectionError):
            await stream.__anext__()

    with instrumented() as traced:
        asyncio.run(run(traced))

    span = traced.one()
    assert span.status.description == "ConnectionError: connection dropped"
    assert "exa.cancelled" not in attrs(span)


# --- get_contents with Result objects ---------------------------------------


def test_capture_urls_reads_only_the_url_of_result_objects(fake):
    client = _client(fake)
    results = client.search("seed").results
    with instrumented(capture_urls=True) as traced:
        client.get_contents(results)

    span_attributes = attrs(traced.one())
    assert span_attributes["fi.retrieval.url_count"] == len(results)
    assert list(span_attributes["fi.retrieval.urls"]) == [result.url for result in results]
    assert RESULT_TITLE not in traced.wire()


# --- get_contents error text (verify r3 V5) --------------------------------


def test_hidden_inputs_remove_requested_urls_from_error_text(fake):
    urls = [
        "https://example.com/{0}?token=t-1".format(ECHO_QUERY),
        "https://example.com/b?key={0}".format(EXA_KEY),
    ]
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        with pytest.raises(ValueError) as caught:
            _client(fake).get_contents(urls)

    assert urls[0] in str(caught.value)
    span = traced.one()
    assert "example.com" not in traced.wire()
    assert EXA_KEY not in traced.wire()
    assert HIDDEN in span.status.description
    assert attrs(span)["fi.retrieval.url_count"] == 2


def test_requested_urls_stay_in_error_text_by_default_with_the_key_redacted(fake):
    urls = ["https://example.com/{0}?key={1}".format(ECHO_QUERY, EXA_KEY)]
    with instrumented() as traced:
        with pytest.raises(ValueError):
            _client(fake).get_contents(urls)

    description = traced.one().status.description
    assert "https://example.com/{0}?key=[redacted]".format(ECHO_QUERY) in description
    assert EXA_KEY not in traced.wire()


# --- an exception whose str() raises (verify r3 V4) -------------------------


class _Unprintable(Exception):
    def __str__(self) -> str:
        raise RuntimeError("str() failed")


def _raise_unprintable(*args, **kwargs):
    raise _Unprintable()


def _assert_unprintable_span(traced) -> None:
    span = traced.one()
    assert span.end_time is not None
    assert span.status.description == "_Unprintable"
    event = _exception_event(span)
    assert event["exception.type"].endswith("_Unprintable")
    assert event["exception.message"] == "[redacted]"
    assert event["exception.stacktrace"] == "[redacted]"


def test_unprintable_error_is_reraised_unchanged_and_the_span_ends():
    from traceai_exa._wrappers import OperationWrapper

    with instrumented() as traced:
        wrapper = OperationWrapper(_tracer(traced), "exa.search")
        with pytest.raises(_Unprintable):
            wrapper(_raise_unprintable, None, ("q",), {})

    _assert_unprintable_span(traced)


def test_unprintable_async_error_is_reraised_unchanged_and_the_span_ends():
    from traceai_exa._wrappers import AsyncOperationWrapper

    async def wrapped(*args, **kwargs):
        raise _Unprintable()

    async def run(traced) -> None:
        wrapper = AsyncOperationWrapper(_tracer(traced), "exa.search")
        with pytest.raises(_Unprintable):
            await wrapper(wrapped, None, ("q",), {})

    with instrumented() as traced:
        asyncio.run(run(traced))

    _assert_unprintable_span(traced)


def test_unprintable_mid_stream_error_ends_the_stream_span():
    from traceai_exa._wrappers import StreamWrapper

    class _UnprintableStream:
        def __iter__(self):
            yield _Chunk()
            raise _Unprintable()

        def close(self) -> None:
            return None

    with instrumented() as traced:
        wrapper = StreamWrapper(_tracer(traced), "exa.search")
        stream = wrapper(lambda *a, **k: _UnprintableStream(), None, ("q",), {})
        next(stream)
        with pytest.raises(_Unprintable):
            next(stream)

    _assert_unprintable_span(traced)
