"""Whole-response output must not carry request prompt pieces that input settings hide."""
import json

import httpx
import openai
import pytest

from .test_privacy_and_raw_streams import configured_tracing
from .test_sdk_lifecycle import BASE_URL, MODEL, TOKEN

PROMPT_SENTINEL = "SENTINEL_PROMPT_FRAGMENT_3307"
REPLY = "synthetic generated reply"
# Fireworks RawOutput (choices[].raw_output) returns the formatted prompt pieces
# (prompt_fragments) and token ids the model saw.
COMPLETION = {
    "id": "chat-raw", "object": "chat.completion", "created": 1, "model": MODEL,
    "choices": [{
        "index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": REPLY},
        "raw_output": {
            "completion": REPLY,
            "prompt_fragments": [PROMPT_SENTINEL],
            "prompt_token_ids": [1, 2, 3],
        },
    }],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


def _exported(**config):
    def respond(request):
        return httpx.Response(200, json=COMPLETION)

    with configured_tracing(**config) as exporter:
        with openai.OpenAI(
            api_key=TOKEN, base_url=BASE_URL, max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as client:
            result = client.chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": PROMPT_SENTINEL}],
                extra_body={"raw_output": True},
            )
        assert result.choices[0].message.content == REPLY
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        return json.dumps(dict(spans[0].attributes), default=str) + json.dumps(
            [(e.name, dict(e.attributes or {})) for e in spans[0].events], default=str
        )


def test_control_output_recorded_without_settings():
    assert REPLY in _exported()


@pytest.mark.parametrize(
    "config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_rawOutputOmitsRequestPromptUnderInputSettings(config):
    assert PROMPT_SENTINEL not in _exported(**config)
