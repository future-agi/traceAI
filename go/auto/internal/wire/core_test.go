package wire

import (
	"net/http"
	"strings"
	"testing"
)

func TestAdditionalFormats(t *testing.T) {
	cases := []struct {
		name, url, request, response, ct, key string
		want                                  any
	}{
		{"completions", "https://api.openai.com/v1/completions", `{"model":"m","prompt":["a","b"]}`, `{"choices":[{"text":"ok","finish_reason":"stop"}]}`, "application/json", "output.value", "ok"},
		{"images", "https://api.openai.com/v1/images/generations", `{"model":"m","prompt":"cat"}`, `{"data":[{"b64_json":"SECRETIMAGE"}]}`, "application/json", "output.value", `{"data":[{"b64_json":"<omitted: 11 base64 chars>"}]}`},
		{"moderations", "https://api.openai.com/v1/moderations", `{"input":"x"}`, `{"results":[{"flagged":false},{"flagged":true}]}`, "application/json", "traceai.moderation.flagged", true},
		{"gemini_embed", "https://generativelanguage.googleapis.com/v1/models/m:embedContent", `{"content":{"parts":[{"text":"x"}]}}`, `{"embedding":{"values":[0,1]}}`, "application/json", "gen_ai.embeddings.dimension.count", 2},
		{"cohere_embed", "https://api.cohere.com/v2/embed", `{"texts":["x"]}`, `{"embeddings":{"float":[[0,1,2]]},"meta":{"billed_units":{"input_tokens":2}}}`, "application/json", "gen_ai.embeddings.dimension.count", 3},
		{"cohere_v1", "https://api.cohere.ai/v1/chat", `{"model":"m","preamble":"sys","chat_history":[{"role":"CHATBOT","message":"previous"}],"message":"x"}`, `{"text":"ok","meta":{"billed_units":{"input_tokens":2,"output_tokens":3}}}`, "application/json", "gen_ai.usage.total_tokens", int64(5)},
		{"bedrock_invoke", "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke", `{"prompt":"x"}`, `{"completion":"ok"}`, "application/json", "output.value", `{"completion":"ok"}`},
		{"bedrock_anthropic", "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke", `{"messages":[{"role":"user","content":"x"}]}`, `{"content":[{"type":"text","text":"ok"}],"usage":{"input_tokens":1,"output_tokens":2}}`, "application/json", "output.value", "ok"},
		{"ollama_generate", "http://localhost:11434/api/generate", `{"model":"m","prompt":"x"}`, `{"response":"ok","done":true,"prompt_eval_count":1,"eval_count":2}`, "application/json", "output.value", "ok"},
		{"ollama_embed", "http://localhost:11434/api/embed", `{"model":"m","input":"x"}`, `{"embeddings":[[0,1]]}`, "application/json", "gen_ai.embeddings.dimension.count", 2},
		{"cartesia_sse", "https://api.cartesia.ai/tts/sse", `{"model_id":"m","transcript":"hi"}`, "data: {\"type\":\"chunk\",\"data\":\"AQIDBA==\"}\n\n", "text/event-stream", "traceai.audio.output_bytes", 4},
		{"eleven_timestamps", "https://api.elevenlabs.io/v1/text-to-speech/voice/with-timestamps", `{"model_id":"m","text":"hi"}`, `{"audio_base64":"AQIDBA==","alignment":{}}`, "application/json", "traceai.audio.output_bytes", 4},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := Build(Find("POST", tc.url), Request{Method: "POST", URL: tc.url, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: []byte(tc.request)}, Response{Status: 200, Header: http.Header{"Content-Type": []string{tc.ct}}, Body: []byte(tc.response)})
			if r == nil {
				t.Fatal("not captured")
			}
			if r.Attributes[tc.key] != tc.want {
				t.Fatalf("%s=%v want %v", tc.key, r.Attributes[tc.key], tc.want)
			}
			for k, v := range r.Attributes {
				if strings.Contains(k, "vectors") || strings.Contains(str(v), "SECRETIMAGE") || strings.Contains(str(v), "AQIDBA==") {
					t.Fatal("binary/vector bytes leaked")
				}
			}
		})
	}
}
func TestMatcherRules(t *testing.T) {
	cases := map[string]string{"https://api.openai.com/v1/chat/completions": "openai", "https://x.openai.azure.com/v1/chat/completions": "azure.ai.openai", "https://bedrock-runtime.eu-west-1.amazonaws.com/model/m/converse": "aws.bedrock", "https://bedrock-runtime.bad.example/model/m/converse": "", "https://evil.amazonaws.com/model/m/converse": "", "http://localhost:11434/api/chat": "ollama", "https://example.com/api/chat": "", "https://example.com:bad/v1/chat/completions": ""}
	for u, want := range cases {
		m := Find("POST", u)
		if want == "" {
			if m != nil {
				t.Errorf("unexpected match %s", u)
			}
		} else if m == nil || m.Provider != want {
			t.Errorf("wrong provider %s", u)
		}
	}
}
func TestMultipartCapAndBinaryDecoder(t *testing.T) {
	req := Request{Method: "POST", URL: "https://api.deepgram.com/v1/listen?model=nova", Header: http.Header{"Content-Type": []string{"audio/wav"}}, Size: 100}
	r := Build(Find(req.Method, req.URL), req, Response{Status: 200, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: []byte(`{"results":{}}`)})
	if r == nil || r.Attributes["traceai.audio.input_bytes"] != int64(100) {
		t.Fatal("binary request without retained bytes not supported")
	}
	fields, files, truncated := ReadMultipart(strings.NewReader("--b\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper\r\n--b--\r\n"), "multipart/form-data; boundary=b", 2)
	if truncated || fields["model"] != "whisper" || len(files) != 0 {
		t.Fatal("multipart text must use its independent 64 KiB cap")
	}
	for _, ct := range []string{"audio/wav", "video/mp4", "image/png", "application/octet-stream", "application/vnd.amazon.eventstream"} {
		if decodeBody(Response{Header: http.Header{"Content-Type": []string{ct}}, Body: []byte("secret")}).Kind != "binary" {
			t.Errorf("binary decoder buffered %s", ct)
		}
	}
}
