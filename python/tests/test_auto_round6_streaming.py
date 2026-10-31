"""A8 complete-frame observation and A10 output recognition regressions."""

import json

import pytest

from fi_instrumentation.auto.wire import _core


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("tail", [b"", b"\n", b"\r", b"\r\n"])
def test_sse_unterminated_json_is_not_an_observed_frame(newline, tail):
    first = b'data: {"usage":{"prompt_tokens":3}}' + newline * 2
    partial = b'data: {"usage":{"prompt_tokens":9}}' + tail
    assert _core.parse_sse(first + partial) == [(None, {"usage": {"prompt_tokens": 3}})]
    assert _core.parse_sse(partial) == []


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_sse_usage_is_observed_only_when_split_delimiter_completes(newline):
    first = b'data: {"usage":{"prompt_tokens":3}}' + newline * 2
    last = b'data: {"usage":{"prompt_tokens":9}}' + newline * 2
    for cut in range(1, len(last)):
        assert _core.parse_sse(first + last[:cut]) == [(None, {"usage": {"prompt_tokens": 3}})]
    assert _core.parse_sse(first + last) == [
        (None, {"usage": {"prompt_tokens": 3}}),
        (None, {"usage": {"prompt_tokens": 9}}),
    ]


@pytest.mark.parametrize("name,arguments,expected", [
    ("get_weather", "", True), ("", "{}", True), ("", "", False), (None, None, False),
])
def test_responses_function_item_is_output_before_argument_deltas(name, arguments, expected):
    event = {"type": "response.output_item.added", "item": {
        "type": "function_call", "id": "fc-test", "name": name, "arguments": arguments,
    }}
    assert _core._has_output_delta(event) is expected
    event["item"]["type"] = "message"
    assert not _core._has_output_delta(event)


@pytest.mark.parametrize("inline_key,mime_key", [("inlineData", "mimeType"), ("inline_data", "mime_type")])
@pytest.mark.parametrize("mime,data,expected", [
    ("audio/pcm", "AQIDBA==", True), ("AUDIO/PCM", "AQIDBA==", True),
    ("audio/pcm", "", False), ("audio/pcm", None, False),
    ("image/png", "AQIDBA==", False), (None, "AQIDBA==", False),
])
def test_gemini_generated_audio_requires_mime_and_nonempty_payload(inline_key, mime_key, mime, data, expected):
    event = {"candidates": [{"content": {"parts": [{inline_key: {mime_key: mime, "data": data}}]}}]}
    assert _core._has_output_delta(event) is expected


@pytest.mark.parametrize("content,expected", [
    ("Hi", True), (" ", True), ({"text": "Hi"}, True), ([{"text": "Hi"}], True),
    ("", False), ({"text": ""}, False), ([{"text": ""}], False), ({"type": "text"}, False),
])
def test_cohere_content_objects_lists_and_empty_scaffolds(content, expected):
    event = {"type": "content-delta", "delta": {"message": {"content": content}}}
    assert _core._has_output_delta(event) is expected


@pytest.mark.parametrize("kind", ["tool-call-start", "tool-call-delta"])
@pytest.mark.parametrize("as_list", [False, True])
@pytest.mark.parametrize("function,expected", [
    ({"name": "get_weather", "arguments": ""}, True),
    ({"arguments": "{}"}, True),
    ({"name": "", "arguments": ""}, False),
    ({}, False),
])
def test_cohere_tool_objects_lists_and_argument_only_deltas(kind, as_list, function, expected):
    call = {"id": "call-test", "function": function}
    event = {"type": kind, "delta": {"message": {"tool_calls": [call] if as_list else call}}}
    assert _core._has_output_delta(event) is expected


@pytest.mark.parametrize("kind,payload", [
    ("response.output_item.added", {"item": {"type": "function_call", "name": "get_weather"}}),
    ("response.output_text.delta", {"delta": "Hi"}),
    ("content_block_delta", {"delta": {"text": "Hi"}}),
    ("content-delta", {"delta": {"message": {"content": {"text": "Hi"}}}}),
    ("tool-call-start", {"delta": {"message": {"tool_calls": {"function": {"name": "get_weather"}}}}}),
    ("tool-call-delta", {"delta": {"message": {"tool_calls": [{"function": {"arguments": "{}"}}]}}}),
    ("text-generation", {"text": "Hi"}),
])
def test_sse_event_header_reaches_first_output_timing_without_mutating_payload(kind, payload):
    # An ID/role-only frame arrives first; the output frame is split over two chunks.
    prelude = b'data: {"id":"scaffold","role":"assistant"}\n\n'
    frame = ("event: " + kind + "\ndata: " + json.dumps(payload) + "\n\n").encode()
    body = prelude + frame
    response = _core.ResponseData(body=body, chunk_arrivals=[
        (len(prelude), 0.01), (len(body) - 1, 0.02), (len(body), 0.04),
    ])
    before = json.dumps(payload)
    assert _core._first_output_seconds(response, _core.Body("sse")) == 0.04
    assert json.dumps(payload) == before
    assert "type" not in payload
    # Complete JSON without the final delimiter cannot establish the milestone.
    response.body = body[:-1]
    assert _core._first_output_seconds(response, _core.Body("sse")) is None


def test_cohere_summary_is_not_a_new_output_delta():
    assert not _core._has_output_delta({
        "type": "message-end", "delta": {"message": {"content": {"text": "summary"}}},
    })


@pytest.mark.parametrize("kind,data,expected", [
    ("chunk", "UklGRgAAAABXQVZF", True), ("chunk", "", False),
    ("chunk", None, False), ("done", "UklGRgAAAABXQVZF", False),
])
def test_cartesia_audio_chunk_requires_nonempty_generated_data(kind, data, expected):
    assert _core._has_output_delta({"type": kind, "data": data}) is expected


@pytest.mark.parametrize("tail", [b"", b",", b',{"usageMetadata":{"promptTokenCount":9',
                                  ',{"text":"é'.encode()[:-1]])
def test_json_array_keeps_complete_members_not_partial_usage_or_utf8(tail):
    # Quoted delimiters, escaped quotes/backslashes and nested arrays cannot end a frame.
    first = {"candidates": [{"content": {"parts": [{"text": 'é { [ } ] " \\'}]}}],
             "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}}
    raw = b"[" + json.dumps(first, ensure_ascii=False).encode()
    frames = list(_core._json_object_frames(raw + tail))
    assert frames == [(len(raw), first)]
    response = _core.ResponseData(headers={"content-type": "application/json"}, body=raw + tail,
                                 chunk_arrivals=[(len(raw) - 1, 0.01), (len(raw + tail), 0.04)])
    body = _core.decode_response(response)
    assert body.kind == "json" and body.json == [first]
    assert _core._first_output_seconds(response, body) == 0.04


@pytest.mark.parametrize("prefix", [b"", b"[", b'[{"usageMetadata":{"promptTokenCount":9}',
                                     b'{"usage":{"input_tokens":9}'])
def test_partial_json_object_cannot_contribute_a_frame(prefix):
    assert list(_core._json_object_frames(prefix)) == []


def test_json_frames_use_byte_offsets_and_do_not_require_array_closure():
    first = {"candidates": [{"content": {"parts": [{"text": "é☺"}]}}]}
    second = {"usageMetadata": {"promptTokenCount": 4}}
    raw_first = json.dumps(first, ensure_ascii=False).encode()
    raw_second = json.dumps(second).encode()
    raw = b"[" + raw_first + b"," + raw_second
    assert list(_core._json_object_frames(raw)) == [(len(raw_first) + 1, first), (len(raw), second)]
    # A complete singleton is also a frame; an incomplete one is never a snapshot.
    assert list(_core._json_object_frames(raw_second)) == [(len(raw_second), second)]
    assert list(_core._json_object_frames(raw_second[:-1])) == []
