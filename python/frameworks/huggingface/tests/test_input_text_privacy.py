"""Input privacy must also cover provider echoes and embedding input copies."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

from traceai_huggingface._wrappers import (
    _AsyncFeatureExtractionWrapper,
    _AsyncTextGenerationWrapper,
    _FeatureExtractionWrapper,
    _TextGenerationWrapper,
)

MARKER = "synthetic-input-text-privacy-marker"
ANSWER = " generated answer"


def exported(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0].attributes, encode_spans(spans).SerializeToString()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("include_tokens", [False, True])
def test_full_text_stream_input_privacy(
    telemetry, asynchronous, flag, enabled, include_tokens
):
    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("input-privacy"), config=TraceConfig(**{flag: enabled}))
    chunks = ([{"token": {"text": ANSWER, "special": False}}] if include_tokens else [])
    chunks.append({
        "generated_text": MARKER + ANSWER,
        "details": {
            "prefill": [{"text": MARKER}],
            "generated_tokens": 1,
            "finish_reason": "eos_token",
        },
    })
    request = {"prompt": MARKER, "stream": True, "return_full_text": True}

    async def consume():
        async def source():
            for chunk in chunks:
                yield chunk

        async def wrapped(**kwargs):
            return source()

        stream = await _AsyncTextGenerationWrapper(tracer=tracer)(
            wrapped, SimpleNamespace(model="synthetic-model"), (), request
        )
        return [chunk async for chunk in stream]

    if asynchronous:
        received = asyncio.run(consume())
    else:
        received = list(_TextGenerationWrapper(tracer=tracer)(
            lambda **kwargs: iter(chunks), SimpleNamespace(model="synthetic-model"), (), request
        ))
    # Privacy changes telemetry only, never the caller's provider response.
    assert received == chunks
    assert received[-1]["generated_text"] == MARKER + ANSWER
    attrs, wire = exported(exporter)
    assert attrs["huggingface.stream.status"] == "completed"
    assert attrs["gen_ai.usage.output_tokens"] == 1
    assert attrs["gen_ai.usage.input_tokens"] == 1
    assert received[-1]["details"]["prefill"] == [{"text": MARKER}]
    assert attrs["huggingface.finish_reason"] == "eos_token"
    output = json.loads(attrs["output.value"])
    if enabled:
        assert MARKER.encode() not in wire
        assert output["generated_text"] == (ANSWER if include_tokens else "")
        assert "prefill" not in output["details"]
        if include_tokens:
            assert attrs["gen_ai.output.messages.0.message.content"] == ANSWER
        if flag == "hide_input_text":
            assert "input.value" not in attrs
            assert attrs["gen_ai.input.messages.0.message.content"] == "__REDACTED__"
        else:
            assert attrs["input.value"] == "__REDACTED__"
    else:
        assert MARKER.encode() in wire
        assert attrs["input.value"] == MARKER
        assert output["generated_text"] == MARKER + ANSWER
        assert attrs["gen_ai.output.messages.0.message.content"] == MARKER + ANSWER


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("positional", [False, True])
def test_embedding_input_privacy(
    telemetry, asynchronous, flag, enabled, batched, positional
):
    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("input-privacy"), config=TraceConfig(**{flag: enabled}))
    text = [MARKER, "second synthetic text"] if batched else MARKER
    args, kwargs = ((text,), {}) if positional else ((), {"text": text})
    response = [[0.25, 0.5]]

    async def wrapped(*args, **kwargs):
        return response

    if asynchronous:
        result = asyncio.run(_AsyncFeatureExtractionWrapper(tracer=tracer)(
            wrapped, SimpleNamespace(model="synthetic-model"), args, kwargs
        ))
    else:
        result = _FeatureExtractionWrapper(tracer=tracer)(
            lambda *args, **kwargs: response, SimpleNamespace(model="synthetic-model"), args, kwargs
        )
    assert result is response
    attrs, wire = exported(exporter)
    assert attrs["huggingface.texts_count"] == (2 if batched else 1)
    assert attrs["embedding.model_name"] == "synthetic-model"
    if enabled:
        assert MARKER.encode() not in wire
        if flag == "hide_input_text":
            assert "input.value" not in attrs
        else:
            assert attrs["input.value"] == "__REDACTED__"
    else:
        assert MARKER.encode() in wire
        assert attrs["input.value"] == (json.dumps(text) if batched else text)
