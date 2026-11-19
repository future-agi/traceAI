package wire

import (
	"net/http"
	"strings"
	"testing"
)

func TestAnthropicStreamBlocksAndDeltas(t *testing.T) {
	u := "https://api.anthropic.com/v1/messages"
	events := []object{
		{"type": "content_block_start", "index": 0, "content_block": object{"type": "text", "text": "A"}},
		{"type": "content_block_delta", "index": 0, "delta": object{"type": "text_delta", "text": "B"}},
		{"type": "content_block_start", "index": 1, "content_block": object{"type": "text", "text": "C"}},
		{"type": "content_block_delta", "index": 1, "delta": object{"type": "text_delta", "text": "D"}},
	}
	var sse strings.Builder
	for _, event := range events {
		sse.WriteString("data: " + dump(event) + "\n\n")
	}
	r := Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(`{"messages":[]}`)}, Response{Status: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: []byte(sse.String())})
	if r == nil || r.Attributes["output.value"] != "AB\nCD" {
		t.Fatalf("block versus delta concatenation wrong: %+v", r)
	}
}

func TestCohereStreamToolDeltas(t *testing.T) {
	u := "https://api.cohere.com/v2/chat"
	events := []object{
		{"type": "message-start", "id": "co-s"},
		{"type": "tool-call-start", "index": 1, "delta": object{"message": object{"tool_calls": object{"id": "c1", "function": object{"name": "weather", "arguments": ""}}}}},
		{"type": "tool-call-delta", "index": 1, "delta": object{"message": object{"tool_calls": object{"function": object{"arguments": `{"city":`}}}}},
		{"type": "tool-call-delta", "index": 1, "delta": object{"message": object{"tool_calls": object{"function": object{"arguments": `"Oslo"}`}}}}},
		{"type": "message-end", "delta": object{"finish_reason": "TOOL_CALL", "usage": object{"tokens": object{"input_tokens": 3, "output_tokens": 2}}}},
	}
	var sse strings.Builder
	for _, event := range events {
		sse.WriteString("data: " + dump(event) + "\n\n")
	}
	r := Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(`{"messages":[]}`)}, Response{Status: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: []byte(sse.String())})
	prefix := "gen_ai.output.messages.0.message.tool_calls.0.tool_call."
	if r == nil || r.Attributes[prefix+"id"] != "c1" || r.Attributes[prefix+"function.name"] != "weather" || r.Attributes[prefix+"function.arguments"] != `{"city":"Oslo"}` || r.Attributes["gen_ai.usage.total_tokens"] != int64(5) {
		t.Fatalf("Cohere streamed tool lost: %+v", r)
	}
}
