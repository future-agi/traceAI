"""Decoder input details obey input privacy independently of full-text output."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

from traceai_huggingface._wrappers import (
    _AsyncTextGenerationWrapper,
    _TextGenerationWrapper,
)

PROMPT = "synthetic-prefill-prompt-381"
PREFILL = "synthetic-prefill-token-381"
ALTERNATE_PREFILL = "synthetic-alternate-prefill-381"
ANSWER = "synthetic-generated-answer-381"


class Dumpable:
    def __init__(self, data):
        self.data = data
        self.generated_text = data["generated_text"]
        self.details = SimpleNamespace(**data["details"])

    def model_dump(self):
        return self.data


def response_data(alternatives):
    details = {
        "prefill": [{"id": 11, "text": PREFILL, "logprob": None}],
        "tokens": [{"id": 12, "text": ANSWER, "logprob": -0.1, "special": False}],
        "generated_tokens": 1,
        "finish_reason": "eos_token",
        "seed": 7,
    }
    if alternatives:
        details["best_of_sequences"] = [{
            "generated_text": ANSWER + " alternate",
            "prefill": [{"id": 13, "text": ALTERNATE_PREFILL, "logprob": None}],
            "tokens": [{"id": 14, "text": ANSWER + " alternate", "logprob": -0.2, "special": False}],
            "generated_tokens": 1,
            "finish_reason": "length",
            "seed": 8,
        }]
    return {"generated_text": ANSWER, "details": details}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("return_full_text", [False, "omitted"])
@pytest.mark.parametrize("alternatives", [False, True])
@pytest.mark.parametrize("shape", ["dict", "namespace", "model_dump"])
def test_nonstream_decoder_details_privacy(
    telemetry, asynchronous, flag, enabled, return_full_text, alternatives, shape
):
    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("prefill-privacy"), config=TraceConfig(**{flag: enabled}))
    expected = response_data(alternatives)
    data = copy.deepcopy(expected)
    if shape == "namespace":
        details = dict(data["details"])
        if alternatives:
            details["best_of_sequences"] = [SimpleNamespace(**item) for item in details["best_of_sequences"]]
        response = SimpleNamespace(generated_text=ANSWER, details=SimpleNamespace(**details))
    elif shape == "model_dump":
        response = Dumpable(data)
    else:
        response = data
    request = {"prompt": PROMPT, "details": True, "decoder_input_details": True}
    if return_full_text != "omitted":
        request["return_full_text"] = return_full_text

    async def wrapped(**kwargs):
        return response

    if asynchronous:
        received = asyncio.run(_AsyncTextGenerationWrapper(tracer=tracer)(
            wrapped, SimpleNamespace(model="synthetic-model"), (), request
        ))
    else:
        received = _TextGenerationWrapper(tracer=tracer)(
            lambda **kwargs: response, SimpleNamespace(model="synthetic-model"), (), request
        )
    assert received is response
    assert data == expected
    if shape == "namespace":
        assert received.details.prefill == expected["details"]["prefill"]
        if alternatives:
            assert received.details.best_of_sequences[0].prefill == expected["details"]["best_of_sequences"][0]["prefill"]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    wire = encode_spans(spans).SerializeToString()
    attrs = spans[0].attributes
    assert attrs["gen_ai.usage.input_tokens"] == 1
    assert attrs["gen_ai.usage.output_tokens"] == 1
    assert attrs["huggingface.finish_reason"] == "eos_token"
    assert attrs["gen_ai.response.finish_reasons"] == ("eos_token",)
    assert attrs["gen_ai.output.messages.0.message.content"] == ANSWER
    assert ANSWER.encode() in wire
    if enabled:
        assert PREFILL.encode() not in wire
        assert ALTERNATE_PREFILL.encode() not in wire
        assert PROMPT.encode() not in wire
        output = json.loads(attrs["output.value"])
        assert "prefill" not in output["details"]
        assert output["generated_text"] == ANSWER
        assert output["details"]["tokens"] == expected["details"]["tokens"]
        assert output["details"]["seed"] == 7
        if alternatives:
            alternate = dict(expected["details"]["best_of_sequences"][0])
            alternate.pop("prefill")
            assert output["details"]["best_of_sequences"] == [alternate]
        if flag == "hide_inputs":
            assert attrs["input.value"] == "__REDACTED__"
        else:
            assert "input.value" not in attrs
            assert attrs["gen_ai.input.messages.0.message.content"] == "__REDACTED__"
    else:
        assert PREFILL.encode() in wire
        assert PROMPT.encode() in wire
        if alternatives:
            assert ALTERNATE_PREFILL.encode() in wire


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("return_full_text", [False, "omitted"])
def test_stream_decoder_details_privacy(
    telemetry, asynchronous, flag, enabled, return_full_text
):
    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("prefill-stream"), config=TraceConfig(**{flag: enabled}))
    chunk = response_data(alternatives=True)
    expected = copy.deepcopy(chunk)
    request = {"prompt": PROMPT, "stream": True, "details": True}
    if return_full_text != "omitted":
        request["return_full_text"] = return_full_text

    async def consume():
        async def source():
            yield chunk

        async def wrapped(**kwargs):
            return source()

        stream = await _AsyncTextGenerationWrapper(tracer=tracer)(
            wrapped, SimpleNamespace(model="synthetic-model"), (), request
        )
        return [item async for item in stream]

    if asynchronous:
        received = asyncio.run(consume())
    else:
        received = list(_TextGenerationWrapper(tracer=tracer)(
            lambda **kwargs: iter([chunk]), SimpleNamespace(model="synthetic-model"), (), request
        ))
    assert received == [expected]
    assert received[0] is chunk
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    wire = encode_spans(spans).SerializeToString()
    assert attrs["gen_ai.usage.input_tokens"] == 1
    assert attrs["gen_ai.usage.output_tokens"] == 1
    assert attrs["huggingface.finish_reason"] == "eos_token"
    assert attrs["huggingface.stream.status"] == "completed"
    assert attrs["gen_ai.output.messages.0.message.content"] == ANSWER
    if enabled:
        assert PREFILL.encode() not in wire
        assert ALTERNATE_PREFILL.encode() not in wire
        assert PROMPT.encode() not in wire
        output = json.loads(attrs["output.value"])
        assert "prefill" not in output["details"]
        assert "prefill" not in output["details"]["best_of_sequences"][0]
    else:
        assert PREFILL.encode() in wire
        assert ALTERNATE_PREFILL.encode() in wire
        assert PROMPT.encode() in wire
