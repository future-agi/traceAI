"""Extraction URLs must stay private even when the real SDK echoes them in errors."""

import asyncio

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.trace import StatusCode
from tavily import AsyncTavilyClient, BadRequestError, TavilyClient

from _support import attrs, instrumented
from _tavily_fake import ECHO_400, TAVILY_KEY, FakeTavily

URLS = [
    "https://example.invalid/" + ECHO_400 + "?password=synthetic-url-secret",
    "https://example.invalid/private?token=another-synthetic-url-secret",
]


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("hide_inputs", [False, True], ids=["defaults", "hide-inputs"])
@pytest.mark.parametrize("keyword", [False, True], ids=["positional", "keyword"])
@pytest.mark.parametrize("multiple", [False, True], ids=["single-url", "multiple-urls"])
def test_extract_http_error_omits_urls(monkeypatch, mode, hide_inputs, keyword, multiple):
    # Unset, rather than false, exercises the default configuration as documented.
    monkeypatch.delenv("FI_HIDE_INPUTS", raising=False)
    monkeypatch.delenv("FI_HIDE_ERROR_DETAILS", raising=False)
    if hide_inputs:
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    config = TraceConfig()
    assert config.hide_inputs is hide_inputs
    urls = URLS if multiple else URLS[0]
    expected_urls = URLS if multiple else URLS[:1]
    args, kwargs = ((), {"urls": urls}) if keyword else ((urls,), {})

    with FakeTavily() as server:
        async def call_async():
            client = AsyncTavilyClient(api_key=TAVILY_KEY, api_base_url=server.origin)
            try:
                await client.extract(*args, **kwargs)
            finally:
                await client.close()

        with instrumented(config=config) as traced:
            with pytest.raises(BadRequestError) as caught:
                if mode == "async":
                    asyncio.run(call_async())
                else:
                    client = TavilyClient(api_key=TAVILY_KEY, api_base_url=server.origin)
                    try:
                        client.extract(*args, **kwargs)
                    finally:
                        client.close()

        # Control: the SDK really sent the URLs, received the echo, and did not retry.
        assert server.paths() == ["/extract"]
        assert server.bodies()[0]["urls"] == urls
        assert str(caught.value) == "Bad URLs: " + ", ".join(expected_urls)
        assert caught.value.__traceback__ is not None

    span = traced.one()
    assert span.name == "tavily.extract"
    assert attrs(span)["tavily.url_count"] == len(expected_urls)
    assert span.status.status_code is StatusCode.ERROR
    wire = traced.wire()
    for private in (*expected_urls, "synthetic-url-secret", TAVILY_KEY):
        assert private not in wire
    # A newer core may remove even the type from status when inputs are hidden.
    expected_status = None if getattr(config, "hide_error_details", False) else "BadRequestError"
    assert span.status.description == expected_status
    (event,) = span.events
    assert event.name == "exception"
    assert event.attributes["exception.type"].endswith("BadRequestError")
    assert "exception.message" not in event.attributes
    assert "exception.stacktrace" not in event.attributes
