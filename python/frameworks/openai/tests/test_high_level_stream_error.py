"""The high-level helper must retain user exceptions on unfinished streams."""
import asyncio

import pytest
from opentelemetry.trace import StatusCode

from .test_high_level_stream_lifecycle import _response_events, _stream_context
from .test_stream_lifecycle import base_url, client, streaming, tracing


@pytest.mark.parametrize("resource", ["chat", "responses"])
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("tracing,hide_errors", [(False, False), (True, True)], indirect=["tracing"])
def test_high_level_context_exception_finishes_once(tracing, base_url, resource, use_async, hide_errors):
    provider, exporter = tracing
    handler = streaming if resource == "chat" else _response_events
    if use_async:
        async def run():
            async with client(handler, base_url, True) as sdk:
                with pytest.raises(ValueError, match="synthetic consumer error"):
                    async with _stream_context(sdk, resource) as stream:
                        await stream.__anext__()
                        raise ValueError("synthetic consumer error")
                assert stream._response.is_closed
        asyncio.run(run())
    else:
        with client(handler, base_url) as sdk:
            with pytest.raises(ValueError, match="synthetic consumer error"):
                with _stream_context(sdk, resource) as stream:
                    next(stream)
                    raise ValueError("synthetic consumer error")
            assert stream._response.is_closed
    assert provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].attributes["gen_ai.provider.name"] == "baseten"
    if hide_errors:
        assert "synthetic consumer error" not in spans[0].to_json()
