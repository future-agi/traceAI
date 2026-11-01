"""Boundary cases for the native message privacy adapter."""

import json

import pytest
from fi_instrumentation import REDACTED_VALUE, TraceConfig

from traceai_mistralai._privacy import _NativeTraceConfig


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize(
    "value",
    [
        "not-json-private-marker",
        '{"parts":"private-marker"}',
        '[{"parts":["private-marker"]}]',
        '[{"parts":null}]',
        "null",
        123,
    ],
)
def test_unparseable_messages_are_not_forwarded(direction, value):
    config = _NativeTraceConfig(TraceConfig(**{f"hide_{direction}_text": True}))
    assert config.mask(f"gen_ai.{direction}.messages", value) is None


@pytest.mark.parametrize("direction", ["input", "output"])
def test_whole_direction_privacy_takes_precedence(direction):
    key = f"gen_ai.{direction}.messages"
    payload = json.dumps(
        [
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": "private-marker"}],
            }
        ]
    )
    for flag in (f"hide_{direction}s", f"hide_{direction}_messages"):
        base = TraceConfig(**{flag: True})
        assert _NativeTraceConfig(base).mask(key, payload) == base.mask(key, payload)


@pytest.mark.parametrize("hidden", [False, True])
def test_input_image_limit_and_environment_config(monkeypatch, hidden):
    monkeypatch.setenv("FI_HIDE_INPUT_IMAGES", str(hidden).lower())
    config = _NativeTraceConfig(TraceConfig(base64_image_max_length=30))
    payload = [
        {
            "role": "user",
            "parts": [
                {"type": "text", "content": "visible-text"},
                {
                    "type": "uri",
                    "modality": "image",
                    "uri": "data:image/png;base64," + "A" * 64,
                },
            ],
        }
    ]
    result = json.loads(config.mask("gen_ai.input.messages", json.dumps(payload)))[0][
        "parts"
    ]
    assert result[0] == payload[0]["parts"][0]
    if hidden:
        assert len(result) == 1
    else:
        assert result[1] == {"type": "uri", "modality": "image", "uri": REDACTED_VALUE}


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("direction", ["input", "output"])
def test_tool_image_environment_and_length_boundaries(monkeypatch, hidden, direction):
    monkeypatch.setenv("FI_HIDE_INPUT_IMAGES", str(hidden).lower())
    short = "data:image/png;base64," + "A" * 8
    long = short + "A"
    url = "https://example.invalid/" + "a" * 80
    config = _NativeTraceConfig(TraceConfig(base64_image_max_length=len(short)))
    chunks = [
        {"type": "text", "text": "visible-text"},
        {"type": "image_url", "image_url": short},
        {"type": "image_url", "image_url": {"url": long, "detail": "low"}},
        {"type": "image_url", "image_url": {"url": url}},
        {"type": "text", "text": "trailing-text"},
    ]
    payload = json.dumps(
        [
            {
                "role": "tool",
                "parts": [
                    {"type": "tool_call_response", "id": "call-1", "response": chunks}
                ],
            }
        ]
    )
    result = json.loads(config.mask(f"gen_ai.{direction}.messages", payload))[0][
        "parts"
    ][0]
    assert result["id"] == "call-1"
    if direction == "output":
        assert result["response"] == chunks
    elif hidden:
        assert result["response"] == [chunks[0], chunks[-1]]
    else:
        assert result["response"] == [
            chunks[0],
            chunks[1],
            {
                "type": "image_url",
                "image_url": {"url": REDACTED_VALUE, "detail": "low"},
            },
            chunks[3],
            chunks[4],
        ]


@pytest.mark.parametrize(
    "image", [None, 123, {}, {"url": None}, {"url": ["private-image"]}]
)
def test_malformed_tool_images_are_not_forwarded(image):
    config = _NativeTraceConfig(TraceConfig(hide_input_images=True))
    payload = json.dumps(
        [
            {
                "role": "tool",
                "parts": [
                    {
                        "type": "tool_call_response",
                        "id": "call-1",
                        "response": [
                            {"type": "image_url", "image_url": image},
                            {"type": "text", "text": "visible-text"},
                        ],
                    }
                ],
            }
        ]
    )
    result = json.loads(config.mask("gen_ai.input.messages", payload))[0]["parts"][0]
    assert result["id"] == "call-1"
    assert result["response"] == [{"type": "text", "text": "visible-text"}]


def test_unrelated_attributes_and_empty_messages_are_preserved():
    config = _NativeTraceConfig(TraceConfig(hide_input_text=True))
    assert config.mask("gen_ai.request.model", "mistral-test") == "mistral-test"
    assert config.mask("gen_ai.input.messages", lambda: "[]") == "[]"
