"""Streamed tool arguments use the same indexed message schema as full replies."""

import json

import openai
import pytest
from openai.types.chat import ChatCompletionChunk
from traceai_openai._response_accumulator import _ChatCompletionAccumulator
from traceai_openai._response_attributes_extractor import _ResponseAttributesExtractor


@pytest.mark.parametrize(
    "usage", [None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}]
)
def test_interleaved_tools_and_choices_keep_complete_arguments(usage):
    accumulator = _ChatCompletionAccumulator(
        request_parameters={"model": "requested-model"},
        chat_completion_type=openai.types.chat.ChatCompletion,
        response_attributes_extractor=_ResponseAttributesExtractor(openai=openai),
    )

    def feed(choices):
        accumulator.process_chunk(
            ChatCompletionChunk(
                id="c",
                created=1,
                model="served-model",
                object="chat.completion.chunk",
                choices=choices,
                usage=usage,
            )
        )

    feed(
        [
            {
                "index": choice,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": tool,
                            "id": f"call_{choice}_{tool}",
                            "type": "function",
                            "function": {
                                "name": f"lookup_{choice}_{tool}",
                                "arguments": '{"query": "',
                            },
                        }
                        for tool in (1, 0)
                    ],
                },
            }
            for choice in (1, 0)
        ]
    )
    # Reading a partial result must not freeze the cache before later chunks.
    dict(accumulator.get_attributes())
    feed(
        [
            {
                "index": choice,
                "delta": {
                    "tool_calls": [
                        {
                            "index": tool,
                            "function": {"arguments": f"value-{choice}-{tool}" + '"}'},
                        }
                        for tool in (0, 1)
                    ]
                },
            }
            for choice in (0, 1)
        ]
    )
    attrs = dict(accumulator.get_extra_attributes())
    for choice in (0, 1):
        for tool in (0, 1):
            prefix = (
                f"gen_ai.output.messages.{choice}.message.tool_calls.{tool}.tool_call."
            )
            assert attrs[prefix + "id"] == f"call_{choice}_{tool}"
            assert attrs[prefix + "function.name"] == f"lookup_{choice}_{tool}"
            assert json.loads(attrs[prefix + "function.arguments"]) == {
                "query": f"value-{choice}-{tool}"
            }
    output = dict(accumulator.get_attributes())
    assert output["output.mime_type"] == "application/json"
    assert len(json.loads(output["output.value"])) == 2
    # Preserve the existing requested-model policy for streamed calls.
    assert "gen_ai.request.model" not in attrs


def test_text_stream_with_explicit_null_usage_still_has_output():
    accumulator = _ChatCompletionAccumulator(
        request_parameters={},
        chat_completion_type=openai.types.chat.ChatCompletion,
    )
    accumulator.process_chunk(
        ChatCompletionChunk(
            id="c",
            created=1,
            model="test-model",
            object="chat.completion.chunk",
            choices=[{"index": 0, "delta": {"role": "assistant", "content": "Hello"}}],
            usage=None,
        )
    )
    assert dict(accumulator.get_attributes()) == {"output.value": "Hello"}
