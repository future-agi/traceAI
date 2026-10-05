"""Privacy: the Tavily key is redacted wherever the SDK keeps it; queries are capped.

tavily-python 0.8.4 keeps the key in different places per client:

* TavilyClient: ``api_key``, ``headers["Authorization"]`` and the requests
  session's ``Authorization`` header (a caller's own session may carry one
  that ``api_key`` does not).
* AsyncTavilyClient: only the httpx client's ``Authorization`` header; there
  is no ``api_key`` attribute.

Each test puts the key into the recorded text, so a redaction that reads the
wrong place fails.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

import pytest

pytest.importorskip("tavily", reason="tavily-python must be installed to test its instrumentor")

from _support import attrs, instrumented  # noqa: E402
from _tavily_fake import ECHO_400, TAVILY_KEY, FakeTavily  # noqa: E402

OTHER_KEY = "tvly-session-key-must-not-be-exported"


@pytest.fixture()
def fake():
    with FakeTavily() as server:
        yield server


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def _wire(traced: Any) -> str:
    return traced.wire()


def test_sync_query_with_the_key_is_redacted(fake):
    from tavily import TavilyClient

    with instrumented() as traced:
        TavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin).search(
            "find {0} now".format(TAVILY_KEY)
        )

    assert attrs(traced.one())["input.value"] == "find [redacted] now"
    assert TAVILY_KEY not in _wire(traced)
    # The request itself is unchanged: redaction is for the span only.
    assert fake.bodies()[0]["query"] == "find {0} now".format(TAVILY_KEY)


def test_async_query_with_the_key_is_redacted(fake):
    """AsyncTavilyClient has no api_key attribute; the key is in its httpx headers."""
    from tavily import AsyncTavilyClient

    async def call() -> None:
        client = AsyncTavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)
        assert not hasattr(client, "api_key")
        try:
            await client.search("find {0} now".format(TAVILY_KEY))
        finally:
            await client.close()

    with instrumented() as traced:
        asyncio.run(call())

    assert attrs(traced.one())["input.value"] == "find [redacted] now"
    assert TAVILY_KEY not in _wire(traced)


def test_env_key_is_redacted(fake, monkeypatch):
    from tavily import TavilyClient

    monkeypatch.setenv("TAVILY_API_KEY", TAVILY_KEY)
    with instrumented() as traced:
        TavilyClient(api_base_url=fake.origin).search("q " + TAVILY_KEY)

    assert attrs(traced.one())["input.value"] == "q [redacted]"


def test_key_on_a_callers_requests_session_is_redacted(fake):
    import requests
    from tavily import TavilyClient

    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + OTHER_KEY
    with instrumented() as traced:
        client = TavilyClient(api_base_url=fake.origin, session=session)
        assert client.api_key is None
        client.search("q " + OTHER_KEY)

    assert fake.calls[0][1]["authorization"] == "Bearer " + OTHER_KEY
    assert attrs(traced.one())["input.value"] == "q [redacted]"
    assert OTHER_KEY not in _wire(traced)


def test_key_on_a_callers_httpx_client_is_redacted(fake):
    import httpx
    from tavily import AsyncTavilyClient

    async def call() -> None:
        async with httpx.AsyncClient(
            base_url=fake.origin, headers={"Authorization": "Bearer " + OTHER_KEY}
        ) as http:
            await AsyncTavilyClient(client=http).extract(
                ["https://example.com/a"], query="q " + OTHER_KEY
            )

    with instrumented() as traced:
        asyncio.run(call())

    assert attrs(traced.one())["input.value"] == "q [redacted]"
    assert OTHER_KEY not in _wire(traced)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_key_in_an_error_message_is_redacted_from_status_and_event(fake, mode):
    """The fake's 400 repeats the query; the caller still sees the original error."""
    from tavily import AsyncTavilyClient, BadRequestError, TavilyClient

    query = "{0} {1}".format(ECHO_400, TAVILY_KEY)

    async def call_async() -> None:
        client = AsyncTavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)
        try:
            await client.search(query)
        finally:
            await client.close()

    with instrumented() as traced:
        with pytest.raises(BadRequestError) as info:
            if mode == "sync":
                TavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin).search(query)
            else:
                asyncio.run(call_async())

    assert TAVILY_KEY in str(info.value)  # unchanged for the caller
    span = traced.one()
    assert span.status.description == "BadRequestError: Bad query: {0} [redacted]".format(
        ECHO_400
    )
    (event,) = [event for event in span.events if event.name == "exception"]
    assert event.attributes["exception.message"] == "Bad query: {0} [redacted]".format(ECHO_400)
    assert "[redacted]" in event.attributes["exception.stacktrace"]
    assert TAVILY_KEY not in _wire(traced)
    assert "tvly-" not in _wire(traced)


PRIVATE = "private question"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("hide", ["config", "env"])
@pytest.mark.parametrize("keyed", [True, False], ids=["keyed", "keyless"])
def test_hidden_inputs_remove_the_query_from_error_text(fake, monkeypatch, mode, hide, keyed):
    """The fake's 400 repeats the query; with inputs hidden it must not leave."""
    from fi_instrumentation import TraceConfig
    from tavily import AsyncTavilyClient, BadRequestError, TavilyClient

    api_key = TAVILY_KEY if keyed else None
    query = "{0} {1}".format(ECHO_400, PRIVATE) + (" " + TAVILY_KEY if keyed else "")
    options: Dict[str, Any] = {}
    if hide == "config":
        options["config"] = TraceConfig(hide_inputs=True)
    else:
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")

    async def call_async() -> None:
        client = AsyncTavilyClient(api_key=api_key, api_base_url=fake.origin)
        try:
            await client.search(query)
        finally:
            await client.close()

    with instrumented(**options) as traced:
        with pytest.raises(BadRequestError) as info:
            if mode == "sync":
                TavilyClient(api_key=api_key, api_base_url=fake.origin).search(query)
            else:
                asyncio.run(call_async())

    assert str(info.value) == "Bad query: " + query  # unchanged for the caller
    span = traced.one()
    assert attrs(span)["input.value"] == "__REDACTED__"
    assert span.status.description == "BadRequestError: Bad query: __REDACTED__"
    (event,) = [event for event in span.events if event.name == "exception"]
    assert event.attributes["exception.message"] == "Bad query: __REDACTED__"
    assert "Bad query: __REDACTED__" in event.attributes["exception.stacktrace"]
    wire = _wire(traced)
    assert PRIVATE not in wire
    assert "tvly-" not in wire


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_without_hidden_inputs_error_text_keeps_the_query(fake, mode):
    from tavily import AsyncTavilyClient, BadRequestError, TavilyClient

    query = "{0} {1}".format(ECHO_400, PRIVATE)

    async def call_async() -> None:
        client = AsyncTavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)
        try:
            await client.search(query)
        finally:
            await client.close()

    with instrumented() as traced:
        with pytest.raises(BadRequestError):
            if mode == "sync":
                TavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin).search(query)
            else:
                asyncio.run(call_async())

    span = traced.one()
    assert attrs(span)["input.value"] == query
    assert span.status.description == "BadRequestError: Bad query: " + query
    (event,) = [event for event in span.events if event.name == "exception"]
    assert event.attributes["exception.message"] == "Bad query: " + query
    assert "Bad query: " + query in event.attributes["exception.stacktrace"]


def test_hidden_inputs_leave_error_text_alone_for_an_empty_query():
    """An empty query has nothing to remove; the error text stays readable."""
    import requests
    from fi_instrumentation import TraceConfig
    from tavily import TavilyClient

    # Nothing listens on port 1, so the call fails before any response.
    client = TavilyClient(api_key=TAVILY_KEY, api_base_url="http://127.0.0.1:1")
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        with pytest.raises(requests.ConnectionError) as info:
            client.search("")

    span = traced.one()
    assert span.status.description.startswith("ConnectionError: ")
    assert "__REDACTED__" not in span.status.description
    (event,) = [event for event in span.events if event.name == "exception"]
    assert "__REDACTED__" not in event.attributes["exception.message"]
    assert event.attributes["exception.message"] == str(info.value)[:1024]


def test_query_is_capped_at_1024_utf8_bytes_on_a_character_boundary(fake):
    from tavily import TavilyClient

    queries = {
        "two-byte": "é" * 2000,
        "three-byte": "a" + "€" * 500,
        "four-byte": "ab" + "\U0001F600" * 300,
    }
    expected = {
        "two-byte": "é" * 512,
        "three-byte": "a" + "€" * 341,
        "four-byte": "ab" + "\U0001F600" * 255,
    }
    with instrumented() as traced:
        client = TavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)
        for query in queries.values():
            client.search(query)

    values = [attrs(span)["input.value"] for span in traced.spans()]
    assert values == list(expected.values())
    for value in values:
        assert len(value.encode("utf-8")) <= 1024
    # The vendor got the full query.
    assert [body["query"] for body in fake.bodies()] == list(queries.values())


def test_redaction_happens_before_the_cap(fake):
    """A key that straddles the 1 KB cut must not leave a prefix behind."""
    from tavily import TavilyClient

    query = "x" * 1015 + TAVILY_KEY
    with instrumented() as traced:
        TavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin).search(query)

    value = attrs(traced.one())["input.value"]
    assert value == "x" * 1015 + "[redacted"
    assert "tvly-" not in json.dumps(value)
