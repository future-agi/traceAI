package wire

import (
	"context"
	"net/http"
	"strings"
	"testing"
)

func TestTextTranscriptionIsNotJSON(t *testing.T) {
	for _, format := range []string{"text", "srt", "vtt"} {
		for _, ct := range []string{"text/plain", "application/json", ""} {
			for _, spoken := range []string{`123`, `true`, `"hello"`, `{"literal":"words"}`} {
				t.Run(format+"/"+ct+"/"+spoken, func(t *testing.T) {
					u := "https://api.openai.com/v1/audio/transcriptions"
					r := Build(Find("POST", u), Request{Method: "POST", URL: u, Fields: map[string]string{"model": "whisper-1", "response_format": format}}, Response{Status: 200, Header: http.Header{"Content-Type": {ct}}, Body: []byte(spoken)})
					if r == nil || r.Attributes["output.value"] != spoken || r.Attributes["gen_ai.audio.transcript"] != spoken {
						t.Fatalf("literal transcription lost: %+v", r)
					}
				})
			}
		}
	}
	// Content-Type is authoritative even without response_format in the request.
	u := "https://api.openai.com/v1/audio/transcriptions"
	r := Build(Find("POST", u), Request{Method: "POST", URL: u, Fields: map[string]string{"model": "whisper-1"}}, Response{Status: 200, Header: http.Header{"Content-Type": {"text/plain"}}, Body: []byte(`123`)})
	if r == nil || r.Attributes["output.value"] != "123" {
		t.Fatal("text/plain sniffed as JSON")
	}
}

func TestSSEAllLineEndings(t *testing.T) {
	for _, newline := range []string{"\n", "\r\n", "\r"} {
		u := "https://api.openai.com/v1/chat/completions"
		sse := ": comment\ndata: {\"choices\":[{\"index\":0,\ndAtA: ignored\ndata: \"delta\":{\"content\":\"hello\"}}]}\n\ndata: [DONE]\n\n"
		r := Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(`{"messages":[]}`)}, Response{Status: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: []byte(strings.ReplaceAll(sse, "\n", newline))})
		if r == nil || r.Attributes["output.value"] != "hello" {
			t.Fatalf("SSE line ending %q lost: %+v", newline, r)
		}
	}
}

func TestResponsesStreamPartialFunctionCalls(t *testing.T) {
	for _, terminal := range []string{"timeout", "close", "done", "final"} {
		t.Run(terminal, func(t *testing.T) {
			u := "https://api.openai.com/v1/responses"
			events := []object{
				{"type": "response.created", "response": object{"id": "r1", "model": "m"}},
				{"type": "response.output_item.added", "output_index": 1, "item": object{"type": "function_call", "id": "fc1", "call_id": "call1", "name": "weather", "arguments": ""}},
				{"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "fc1", "delta": `{"city":`},
				{"type": "response.function_call_arguments.delta", "item_id": "fc1", "delta": `"Oslo"}`},
			}
			if terminal == "done" || terminal == "final" {
				events = append(events, object{"type": "response.function_call_arguments.done", "output_index": 1, "arguments": `{"city":"Oslo"}`}, object{"type": "response.output_item.done", "output_index": 1, "item": object{"type": "function_call", "id": "fc1", "call_id": "call1", "name": "weather", "arguments": `{"city":"Oslo"}`}})
			}
			if terminal == "final" {
				events = append(events, object{"type": "response.completed", "response": object{"id": "r1", "status": "completed", "output": []any{object{"type": "function_call", "call_id": "call1", "name": "weather", "arguments": `{"city":"Oslo"}`}}}})
			}
			var sse strings.Builder
			for _, e := range events {
				sse.WriteString("data: " + dump(e) + "\n\n")
			}
			resp := Response{Status: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: []byte(sse.String()), Incomplete: terminal == "close"}
			if terminal == "timeout" {
				resp.Error = context.DeadlineExceeded
			}
			r := Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(`{"model":"m"}`)}, resp)
			prefix := "gen_ai.output.messages.0.message.tool_calls.0.tool_call."
			if r == nil || r.OK != (terminal != "timeout") || r.Attributes[prefix+"id"] != "call1" || r.Attributes[prefix+"function.name"] != "weather" || r.Attributes[prefix+"function.arguments"] != `{"city":"Oslo"}` || !strings.Contains(str(r.Attributes["output.value"]), "weather") {
				t.Fatalf("partial function response lost: %+v", r)
			}
			if terminal == "timeout" && r.Attributes["error.type"] == nil {
				t.Fatal("missing error type")
			}
		})
	}
}

func TestPollingRequiresCompletedJSON(t *testing.T) {
	u := "https://api.assemblyai.com/v2/transcript/id"
	for _, raw := range []string{"", "processing", `"processing"`, `{}`, `[]`, `{"status":"processing"}`, `{"status":"completed"}`, `{"status":"error"}`} {
		r := Build(Find("GET", u), Request{Method: "GET", URL: u}, Response{Status: 200, Body: []byte(raw)})
		want := strings.Contains(raw, `"completed"`) || strings.Contains(raw, `"error"`)
		if (r != nil) != want {
			t.Fatalf("polling %q captured=%v", raw, r != nil)
		}
	}
}

func TestMultipartChatModel(t *testing.T) {
	u := "https://api.openai.com/v1/chat/completions"
	r := Build(Find("POST", u), Request{Method: "POST", URL: u, Fields: map[string]string{"model": "m", "messages": `[{"role":"user","content":"hi"}]`}}, Response{Status: 200})
	if r == nil || r.Name != "chat m" || r.Attributes["gen_ai.request.model"] != "m" || r.Attributes["gen_ai.input.messages.0.message.content"] != "hi" {
		t.Fatalf("multipart chat model lost: %+v", r)
	}
}

func TestStreamingEndpointVariants(t *testing.T) {
	for _, tc := range []struct{ url, request, ct, response string }{
		{"https://generativelanguage.googleapis.com/v1beta/models/m:streamGenerateContent", `{"contents":[]}`, "application/json", `[]`},
		{"https://us-east1-aiplatform.googleapis.com/v1/projects/p/locations/l/publishers/anthropic/models/m:streamRawPredict", `{"messages":[]}`, "application/json", `{}`},
		{"https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse-stream", `{"messages":[]}`, "application/vnd.amazon.eventstream", ""},
		{"https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke-with-response-stream", `{}`, "application/vnd.amazon.eventstream", ""},
	} {
		r := Build(Find("POST", tc.url), Request{Method: "POST", URL: tc.url, Body: []byte(tc.request)}, Response{Status: 200, Header: http.Header{"Content-Type": {tc.ct}}, Body: []byte(tc.response)})
		if r == nil || r.Attributes["gen_ai.request.stream"] != true {
			t.Fatalf("stream variant missing for %s: %+v", tc.url, r)
		}
	}
}

func TestMessagePartsVersusStreamDeltas(t *testing.T) {
	for _, tc := range []struct{ url, request, response string }{
		{"https://api.openai.com/v1/chat/completions", `{"messages":[{"content":[{"type":"text","text":"A"},{"type":"image_url","image_url":"ignored"},{"type":"text","text":"B"}]}]}`, `{"choices":[{"message":{"role":"assistant","content":[{"type":"text","text":"C"},{"type":"text","text":"D"}]}}]}`},
		{"https://api.anthropic.com/v1/messages", `{"messages":[{"role":"user","content":[{"type":"text","text":"A"},{"type":"text","text":"B"}]}]}`, `{"content":[{"type":"text","text":"C"},{"type":"text","text":"D"}]}`},
		{"https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse", `{"messages":[{"role":"user","content":[{"text":"A"},{"text":"B"}]}]}`, `{"output":{"message":{"role":"assistant","content":[{"text":"C"},{"text":"D"}]}}}`},
		{"https://generativelanguage.googleapis.com/v1beta/models/m:generateContent", `{"contents":[{"parts":[{"text":"A"},{"text":"B"}]}]}`, `{"candidates":[{"content":{"parts":[{"text":"C"},{"text":"D"}]}}]}`},
		{"https://api.openai.com/v1/responses", `{"input":[{"role":"user","content":[{"type":"input_text","text":"A"},{"type":"input_text","text":"B"}]}]}`, `{"output":[{"type":"message","content":[{"type":"output_text","text":"C"},{"type":"output_text","text":"D"}]}]}`},
	} {
		r := Build(Find("POST", tc.url), Request{Method: "POST", URL: tc.url, Body: []byte(tc.request)}, Response{Status: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: []byte(tc.response)})
		if r == nil || r.Attributes["gen_ai.input.messages.0.message.content"] != "A\nB" || r.Attributes["output.value"] != "C\nD" {
			t.Fatalf("message parts not newline joined for %s: %+v", tc.url, r)
		}
	}
}
