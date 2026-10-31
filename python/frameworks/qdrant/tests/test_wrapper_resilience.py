"""Result extraction must stay bounded and must not alter SDK results/errors."""

import json
import logging
from types import SimpleNamespace

import pytest

from traceai_qdrant._wrappers import QueryPointsWrapper


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
async def test_metadata_is_bounded_and_fail_open(is_async, tracer_provider, in_memory_span_exporter, caplog):
    wrapper = QueryPointsWrapper(tracer_provider.get_tracer("resilience"), "query_batch_points")
    result = [SimpleNamespace(points=[SimpleNamespace(score=0.5)] * 10000)] * 2
    def sync(collection_name, requests):
        return result
    async def async_method(collection_name, requests):
        return result
    async def invoke():
        if is_async:
            return await wrapper.async_call(async_method, None, ("docs", []), {})
        return wrapper(sync, None, ("docs", []), {})
    assert await invoke() is result
    (span,) = in_memory_span_exporter.get_finished_spans()
    assert span.attributes["db.vector.results.count"] == 20000
    assert len(json.loads(span.attributes["db.vector.results.scores"])) == 10

    class UnreadableMetadata:
        @property
        def points(self):
            raise ValueError("unsupported result metadata")
    result = [UnreadableMetadata()]
    in_memory_span_exporter.clear()
    with caplog.at_level(logging.DEBUG, logger="traceai_qdrant._wrappers"):
        assert await invoke() is result
    (span,) = in_memory_span_exporter.get_finished_spans()
    assert span.status.status_code.name == "OK"
    assert any(record.exc_info and "result metadata" in record.message for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
async def test_sdk_exception_identity_and_bad_arguments(is_async, tracer_provider, in_memory_span_exporter, caplog):
    wrapper = QueryPointsWrapper(tracer_provider.get_tracer("resilience"))
    original_error = RuntimeError("offline SDK failure")
    def sync(collection_name):
        raise original_error
    async def async_method(collection_name):
        raise original_error
    with pytest.raises(RuntimeError) as exc:
        if is_async:
            await wrapper.async_call(async_method, None, ("docs",), {})
        else:
            wrapper(sync, None, ("docs",), {})
    assert exc.value is original_error
    (span,) = in_memory_span_exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert len(span.events) == 1
    in_memory_span_exporter.clear()
    with caplog.at_level(logging.DEBUG, logger="traceai_qdrant._wrappers"), pytest.raises(TypeError, match="collection_name"):
        if is_async:
            await wrapper.async_call(async_method, None, (), {})
        else:
            wrapper(sync, None, (), {})
    (span,) = in_memory_span_exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert any(record.exc_info and "request metadata" in record.message for record in caplog.records)
