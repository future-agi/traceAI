"""Older SDK dataclasses retain structured generated metadata under input privacy."""

import asyncio
import copy
import json
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace

import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

from traceai_huggingface._wrappers import (
    _AsyncTextGenerationWrapper,
    _TextGenerationWrapper,
)


class FinishReason(str, Enum):
    EOS = "eos_token"
    LENGTH = "length"


@dataclass
class Token:
    id: int
    text: str
    logprob: float
    special: bool


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text"])
@pytest.mark.parametrize("enabled", [False, True])
def test_typed_generated_details_remain_structured(telemetry, asynchronous, flag, enabled):
    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("typed-decoder"), config=TraceConfig(**{flag: enabled}))
    marker = "synthetic-typed-prefill-381"
    answer = "synthetic-typed-answer-381"
    response = {
        "generated_text": answer,
        "details": {
            "prefill": [{"text": marker}],
            "tokens": [Token(1, answer, -0.1, False)],
            "finish_reason": FinishReason.EOS,
            "generated_tokens": 1,
            "best_of_sequences": [{
                "generated_text": answer + " alternate",
                "prefill": [{"text": marker}],
                "tokens": [Token(2, answer + " alternate", -0.2, False)],
                "finish_reason": FinishReason.LENGTH,
                "generated_tokens": 1,
            }],
        },
    }
    expected = copy.deepcopy(response)
    request = {"prompt": marker, "details": True, "decoder_input_details": True}

    async def wrapped(**kwargs):
        return response

    if asynchronous:
        result = asyncio.run(_AsyncTextGenerationWrapper(tracer=tracer)(
            wrapped, SimpleNamespace(model="synthetic-model"), (), request
        ))
    else:
        result = _TextGenerationWrapper(tracer=tracer)(
            lambda **kwargs: response, SimpleNamespace(model="synthetic-model"), (), request
        )
    assert result is response
    assert response == expected
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    wire = encode_spans(spans).SerializeToString()
    output = json.loads(attrs["output.value"])
    assert attrs["gen_ai.usage.input_tokens"] == 1
    assert attrs["gen_ai.usage.output_tokens"] == 1
    assert attrs["gen_ai.output.messages.0.message.content"] == answer
    if enabled:
        assert output["details"]["tokens"][0]["text"] == answer
        assert output["details"]["tokens"][0] == {"id": 1, "text": answer, "logprob": -0.1, "special": False}
        assert output["details"]["finish_reason"] == "eos_token"
        alternative = output["details"]["best_of_sequences"][0]
        assert alternative["tokens"][0] == {"id": 2, "text": answer + " alternate", "logprob": -0.2, "special": False}
        assert alternative["finish_reason"] == "length"
        assert "prefill" not in output["details"]
        assert "prefill" not in alternative
        assert marker.encode() not in wire
    else:
        assert marker.encode() in wire
        assert answer.encode() in wire
