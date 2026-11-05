"""Real-client search regressions for endpoint credentials and capped PII."""

import asyncio
import base64

import httpx
import pytest
import requests
from fi_instrumentation import TraceConfig
from opentelemetry.trace import StatusCode
from tavily import AsyncTavilyClient, TavilyClient

from _support import attrs, instrumented
from _tavily_fake import FAIL_500, TAVILY_KEY, FakeTavily

ENDPOINT_USER = "synthetic-gateway-user"
ENDPOINT_PASSWORD = "synthetic-gateway-password-42"
EMAIL = "jane.doe@example.invalid"


def _search(mode, endpoint, query):
    async def call_async():
        client = AsyncTavilyClient(api_key=TAVILY_KEY, api_base_url=endpoint)
        try:
            return await client.search(query)
        finally:
            await client.close()

    if mode == "async":
        return asyncio.run(call_async())
    client = TavilyClient(api_key=TAVILY_KEY, api_base_url=endpoint)
    try:
        return client.search(query)
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("hide_inputs", [False, True], ids=["defaults", "hide-inputs"])
def test_search_http_error_omits_endpoint_credentials(monkeypatch, mode, hide_inputs):
    monkeypatch.delenv("FI_HIDE_INPUTS", raising=False)
    monkeypatch.delenv("FI_HIDE_ERROR_DETAILS", raising=False)
    if hide_inputs:
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    config = TraceConfig()
    assert config.hide_inputs is hide_inputs

    # Capture the actual HTTP exception to verify the wrapper re-raises that
    # same object, not a sanitized replacement with a different traceback.
    response_class = httpx.Response if mode == "async" else requests.Response
    raise_for_status = response_class.raise_for_status
    original_errors = []

    def capture_error(response, *args, **kwargs):
        try:
            return raise_for_status(response, *args, **kwargs)
        except Exception as error:
            original_errors.append(error)
            raise

    monkeypatch.setattr(response_class, "raise_for_status", capture_error)
    expected_error = httpx.HTTPStatusError if mode == "async" else requests.HTTPError
    with FakeTavily() as server:
        endpoint = server.origin.replace(
            "http://", f"http://{ENDPOINT_USER}:{ENDPOINT_PASSWORD}@"
        )
        with instrumented(config=config) as traced:
            with pytest.raises(expected_error) as caught:
                _search(mode, endpoint, FAIL_500)

        assert server.paths() == ["/search"]
        assert server.bodies()[0]["query"] == FAIL_500
        basic_credentials = base64.b64encode(
            f"{ENDPOINT_USER}:{ENDPOINT_PASSWORD}".encode()
        ).decode()
        assert server.calls[0][1]["authorization"] == "Basic " + basic_credentials
        assert len(original_errors) == 1
        assert caught.value is original_errors[0]
        assert ENDPOINT_PASSWORD in str(caught.value)
        assert caught.value.__traceback__ is not None
        assert caught.value.response.status_code == 500

    span = traced.one()
    assert span.name == "tavily.search"
    assert span.status.status_code is StatusCode.ERROR
    for secret in (ENDPOINT_USER, ENDPOINT_PASSWORD, basic_credentials, TAVILY_KEY):
        assert secret not in traced.wire()
    expected_status = None if getattr(config, "hide_error_details", False) else expected_error.__name__
    assert span.status.description == expected_status
    (event,) = span.events
    assert event.name == "exception"
    assert event.attributes["exception.type"].endswith(expected_error.__name__)
    assert "exception.message" not in event.attributes
    assert "exception.stacktrace" not in event.attributes


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "prefix", ["", "x" * 1010, "é" * 505], ids=["short-control", "ascii-cap", "utf8-cap"]
)
def test_search_masks_pii_before_capping_query(mode, prefix):
    query = prefix + " " + EMAIL
    with FakeTavily() as server:
        with instrumented(config=TraceConfig(pii_redaction=True)) as traced:
            result = _search(mode, server.origin, query)
        assert server.paths() == ["/search"]
        assert server.bodies()[0]["query"] == query
        assert result["query"] == query

    span = traced.one()
    assert span.status.status_code is StatusCode.OK
    values = attrs(span)
    for key in ("input.value", "gen_ai.retrieval.query"):
        value = values[key]
        assert "jane.doe" not in value, f"PII fragment leaked in {key}"
        assert "<EMAIL" in value
        assert len(value.encode("utf-8")) <= 1024
    assert values["input.value"] == values["gen_ai.retrieval.query"]
