"""Low-level SDK raw-stream reader regression probes."""

import pytest
import test_stream_lifecycle as h


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("api", ["with_raw_response", "with_streaming_response"])
@pytest.mark.parametrize("reader", ["iter_bytes", "iter_text", "iter_lines"])
async def test_low_level_stream_consumption_preserves_output(asynchronous, api, reader):
    body = (h.AsyncBody if asynchronous else h.SyncBody)()
    with h.instrumented() as (memory, provider):
        if asynchronous:
            async with h.async_client(body) as client:
                resource = getattr(client.chat.completions, api)
                if api == "with_streaming_response":
                    async with resource.create(**h.PARAMS) as response:
                        assert [v async for v in getattr(response, reader)()]
                else:
                    response = await resource.create(**h.PARAMS)
                    assert [v async for v in getattr(response, reader)()]
        else:
            with h.sync_client(body) as client:
                resource = getattr(client.chat.completions, api)
                if api == "with_streaming_response":
                    with resource.create(**h.PARAMS) as response:
                        assert list(getattr(response, reader)())
                else:
                    response = resource.create(**h.PARAMS)
                    assert list(getattr(response, reader)())
        provider.force_flush()
        (span,) = memory.get_finished_spans()
        assert span.attributes.get("output.value") == "private-output-marker-tail", (
            dict(span.attributes)
        )
