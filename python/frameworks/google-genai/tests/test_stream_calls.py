"""Preserve parallel calls in Google GenAI streams.

The fake response implements only the SDK's serialization boundary; no client or
network calls are needed. Parts deliberately have no index, as in google-genai.
"""

from copy import deepcopy
import json

import pytest

from fi_instrumentation.fi_types import (
    MessageAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from traceai_google_genai._stream import _ResponseAccumulator, _ResponseExtractor


class _Chunk:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, *, exclude_unset, warnings):
        assert exclude_unset is True
        assert warnings is False
        # Returning the original also catches accidental mutation by tracing.
        return self.payload


def _chunk(*parts, index=0):
    return _Chunk({
        "candidates": [{
            "index": index,
            "content": {"role": "model", "parts": list(parts)},
        }],
    })


def _call(name, args, call_id=None):
    function_call = {"name": name, "args": args}
    if call_id is not None:
        function_call["id"] = call_id
    return {"function_call": function_call}


def _assert_recorded_calls(accumulator, expected, candidate_index=0):
    result = accumulator._result()
    parts = result["candidates"][candidate_index]["content"]["parts"]
    assert [part["function_call"] for part in parts if "function_call" in part] == expected

    extractor = _ResponseExtractor(accumulator)
    raw_attributes = dict(extractor.get_attributes())
    assert json.loads(raw_attributes[SpanAttributes.OUTPUT_VALUE]) == result

    attributes = dict(extractor.get_extra_attributes())
    prefix = (
        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{candidate_index}."
        f"{MessageAttributes.MESSAGE_TOOL_CALLS}."
    )
    for index, function_call in enumerate(expected):
        assert attributes[
            f"{prefix}{index}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}"
        ] == function_call["name"]
        if function_call["args"]:
            assert json.loads(attributes[
                f"{prefix}{index}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}"
            ]) == function_call["args"]
    names = [
        value for key, value in attributes.items()
        if key.startswith(prefix) and key.endswith(
            f".{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}"
        )
    ]
    assert names == [function_call["name"] for function_call in expected]


@pytest.mark.parametrize("separate_chunks", [False, True], ids=["one-chunk", "across-chunks"])
@pytest.mark.parametrize("second", [
    _call("clock", {"zone": "UTC"}),
    _call("weather", {"city": "Rome"}),
    _call("weather", {"city": "Paris"}),
], ids=["different-names", "same-name", "identical-calls"])
def test_parallel_function_calls_without_indexes(separate_chunks, second):
    first = _call("weather", {"city": "Paris"})
    chunks = [_chunk(first), _chunk(second)] if separate_chunks else [_chunk(first, second)]
    before = deepcopy([chunk.payload for chunk in chunks])
    accumulator = _ResponseAccumulator()

    for chunk in chunks:
        accumulator.process_chunk(chunk)

    _assert_recorded_calls(accumulator, [first["function_call"], second["function_call"]])
    assert [chunk.payload for chunk in chunks] == before


def test_identified_call_deltas_follow_identity_not_position():
    chunks = [
        _chunk(
            _call("weather", {"city": "Paris"}, "call-paris"),
            _call("weather", {"city": "Rome"}, "call-rome"),
        ),
        _chunk(
            {"function_call": {"id": "call-rome", "args": {"units": "fahrenheit"}}},
            {"function_call": {"id": "call-paris", "args": {"units": "celsius"}}},
        ),
    ]
    before = deepcopy([chunk.payload for chunk in chunks])
    accumulator = _ResponseAccumulator()
    for chunk in chunks:
        accumulator.process_chunk(chunk)

    _assert_recorded_calls(accumulator, [
        {"id": "call-paris", "name": "weather", "args": {"city": "Paris", "units": "celsius"}},
        {"id": "call-rome", "name": "weather", "args": {"city": "Rome", "units": "fahrenheit"}},
    ])
    assert [chunk.payload for chunk in chunks] == before


def test_text_deltas_do_not_hide_function_calls():
    weather = _call("weather", {"city": "Paris"})
    clock = _call("clock", {"zone": "UTC"})
    accumulator = _ResponseAccumulator()
    accumulator.process_chunk(_chunk({"text": "I will "}))
    accumulator.process_chunk(_chunk(weather, {"text": "check."}, clock))

    _assert_recorded_calls(accumulator, [weather["function_call"], clock["function_call"]])
    attributes = dict(_ResponseExtractor(accumulator).get_extra_attributes())
    assert attributes[
        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"
    ] == "I will check."


@pytest.mark.parametrize("args", [{"city": "Paris"}, {}], ids=["with-args", "empty-args"])
def test_single_function_call_control(args):
    call = _call("weather", args)
    accumulator = _ResponseAccumulator()
    accumulator.process_chunk(_chunk(call))
    _assert_recorded_calls(accumulator, [call["function_call"]])
    assert accumulator._result()["candidates"][0]["content"]["parts"] == [call]


def test_call_identity_is_scoped_to_candidate_index():
    accumulator = _ResponseAccumulator()
    first = _chunk(_call("weather", {"city": "Paris"}, "shared-id"), index=0)
    second = _chunk(_call("weather", {"city": "Rome"}, "shared-id"), index=1)
    accumulator.process_chunk(_Chunk({
        "candidates": second.payload["candidates"] + first.payload["candidates"],
    }))
    accumulator.process_chunk(_chunk(
        {"function_call": {"id": "shared-id", "args": {"units": "celsius"}}},
        index=0,
    ))

    _assert_recorded_calls(accumulator, [
        {"id": "shared-id", "name": "weather", "args": {"city": "Paris", "units": "celsius"}},
    ], candidate_index=0)
    _assert_recorded_calls(accumulator, [
        {"id": "shared-id", "name": "weather", "args": {"city": "Rome"}},
    ], candidate_index=1)


def test_empty_stream_and_metadata_control():
    accumulator = _ResponseAccumulator()
    assert accumulator._result() is None
    accumulator.process_chunk(_chunk({"text": "Hello "}))
    accumulator.process_chunk(_chunk({"text": "world"}))
    accumulator.process_chunk(_Chunk({
        "candidates": [{"index": 0, "finish_reason": "STOP"}],
        "usage_metadata": {
            "prompt_token_count": 3,
            "candidates_token_count": 2,
            "total_token_count": 5,
        },
        "model_version": "fake-genai-model",
    }))

    attributes = dict(_ResponseExtractor(accumulator).get_extra_attributes())
    assert attributes[
        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"
    ] == "Hello world"
    assert attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "fake-genai-model"
    assert attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 3
    assert attributes[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 2
    assert attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 5
    assert accumulator._result()["candidates"][0]["finish_reason"] == "STOP"
