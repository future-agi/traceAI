"""Streamed chunks dumped with model_dump() carry None for absent fields."""

from traceai_minimax._response_attributes_extractor import _StreamingChatCompletionResponseExtractor


def _chunk(delta, finish_reason=None, usage=None):
    return {
        "id": "chatcmpl-synthetic",
        "model": "MiniMax-M1",
        "usage": usage,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}],
    }


def test_none_fields_in_stream_chunks_do_not_raise_and_keep_content_and_usage():
    extractor = _StreamingChatCompletionResponseExtractor()
    chunks = [
        _chunk({"role": "assistant", "content": "Hel", "tool_calls": None, "function_call": None}),
        _chunk({"role": None, "content": "lo", "tool_calls": None}),
        _chunk({"content": None, "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                  "function": {"name": "lookup", "arguments": None}}]}),
        _chunk({"content": None, "tool_calls": [{"index": None, "id": None, "type": None,
                                                  "function": {"name": None, "arguments": "{\"q\": 1}"}}]}),
        _chunk({"content": None, "tool_calls": None}, finish_reason="stop",
               usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
        _chunk(None, usage=None),
        {"id": "chatcmpl-synthetic", "model": "MiniMax-M1", "usage": None, "choices": None},
    ]
    for chunk in chunks:
        extractor.process_chunk(chunk)

    attributes = dict(extractor.get_attributes())
    assert 5 in attributes.values()  # total tokens survive later usage=None chunks
    assert extractor._content_parts == ["Hel", "lo"]
    assert extractor._tool_calls[0]["id"] == "call_1"
    assert extractor._tool_calls[0]["function"] == {"name": "lookup", "arguments": "{\"q\": 1}"}
    assert extractor._finish_reason == "stop"
