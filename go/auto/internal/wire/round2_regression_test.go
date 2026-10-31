package wire

import (
	"bytes"
	"compress/gzip"
	"context"
	"net/http"
	"strings"
	"testing"
)

func TestOllamaToolCalls(t *testing.T) {
	for _, ct := range []string{"application/json", "application/x-ndjson"} {
		t.Run(ct, func(t *testing.T) {
			u := "http://localhost:11434/api/chat"
			raw := `{"model":"llama3","message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"get_weather","arguments":{"city":"Oslo"}}}]},"done":true,"done_reason":"stop","prompt_eval_count":4,"eval_count":2}`
			if ct == "application/x-ndjson" {
				raw = `{"message":{"role":"assistant","tool_calls":[{"function":{"name":"get_time","arguments":{}}}]}}` + "\n" + raw + "\n"
			}
			r := Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(`{"model":"llama3","messages":[]}`)}, Response{Status: 200, Header: http.Header{"Content-Type": {ct}}, Body: []byte(raw)})
			index := "0"
			if ct == "application/x-ndjson" {
				index = "1"
			}
			prefix := "gen_ai.output.messages.0.message.tool_calls." + index + ".tool_call.function."
			if r == nil || r.Attributes[prefix+"name"] != "get_weather" || r.Attributes[prefix+"arguments"] != `{"city":"Oslo"}` || !strings.Contains(str(r.Attributes["output.value"]), "get_weather") || r.Attributes["gen_ai.usage.total_tokens"] != int64(6) {
				t.Fatalf("Ollama function-only response lost: %v", r)
			}
		})
	}
}

func TestPartialCredentialJSON(t *testing.T) {
	for _, raw := range []string{
		`{"api_key":"PRIVATE",`,
		`{"nested":[{"authorization_token":"PRIVATE"}],`,
		`{"api_key":"PRIVATE",`,
		`{"secret":{"nested":"PRIVATE"},`,
		`{"token":["PRIVATE"],`,
		`{"password":"PRIVATE`,
		`error details: {"client-secret" : "PRIVATE",`,
		`{"arguments":"{\"api_key\":\"PRIVATE\",",`,
		`{"arguments":"{\"api_key\":\"PRIVATE\",`,
	} {
		got := PrivacyString(raw, nil)
		if strings.Contains(got, "PRIVATE") || !strings.Contains(got, CredentialRedacted) || PrivacyString(got, nil) != got {
			t.Fatalf("partial JSON credential leaked or not idempotent: %q => %q", raw, got)
		}
	}
	for _, suffix := range []string{`",`, ""} {
		raw := `{"data":"` + strings.Repeat("YWJj", 100) + suffix
		got := PrivacyString(raw, nil)
		if strings.Contains(got, "YWJj") || !strings.Contains(got, "<omitted: 400 base64 chars>") {
			t.Fatalf("partial JSON media leaked: %s", got)
		}
	}
	for _, raw := range []string{`{"city": "Oslo",`, `{"text":"a quote: \"city\": safe"`, `{"unfinished":"text`} {
		if got := PrivacyString(raw, nil); got != raw {
			t.Fatalf("noncredential partial JSON changed: %q => %q", raw, got)
		}
	}
	// The transport error occurs after a complete SSE event, but its tool
	// argument string is intentionally unfinished JSON.
	u := "https://api.openai.com/v1/chat/completions"
	event := object{"choices": []any{object{"index": 0, "delta": object{"tool_calls": []any{object{"index": 0, "function": object{"name": "tool", "arguments": `{"api_key":"PRIVATE",`}}}}}}}
	r := Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(`{"messages":[]}`)}, Response{Status: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: []byte("data: " + dump(event) + "\n\n"), Error: context.DeadlineExceeded})
	if r == nil || r.OK || strings.Contains(dump(r.Attributes), "PRIVATE") || r.Attributes["output.value"] == nil {
		t.Fatalf("partial tool credential leaked or output lost: %v", r)
	}
}

func TestPercentEncodedDataURLs(t *testing.T) {
	for _, payload := range []string{strings.Repeat("QUJD%2F", 100), strings.Repeat("%2B%2f%3D", 100), "QUJD%0AQUJD%0D%0A"} {
		for _, input := range []string{"data:image/png;base64," + payload, dump(object{"url": "data:audio/wav;base64," + payload}), "error fetching data:image/png;base64," + payload + " suffix"} {
			got := PrivacyString(input, nil)
			if strings.Contains(got, "QUJD") || strings.Contains(got, "%2") || strings.Contains(got, "%3D") || strings.Contains(got, "%0") || !strings.Contains(got, "<omitted:") || PrivacyString(got, nil) != got {
				t.Fatalf("percent-encoded base64 leaked or scrub is not idempotent: %s", got)
			}
		}
	}
}

func TestCompressedMultipartCore(t *testing.T) {
	for _, host := range []string{"api.openai.com", "proxy.example"} {
		u := "https://" + host + "/v1/audio/transcriptions"
		ct := "multipart/form-data; boundary=b"
		plain := "--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n" + strings.Repeat("A", 10000) + "\r\n--b\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n--b--\r\n"
		var buf bytes.Buffer
		w := gzip.NewWriter(&buf)
		_, _ = w.Write([]byte(plain))
		_ = w.Close()
		r := Build(Find("POST", u), Request{Method: "POST", URL: u, Header: http.Header{"Content-Type": {ct}, "Content-Encoding": {"gzip"}}, Body: buf.Bytes(), MaxBodyBytes: 1024}, Response{Status: 200})
		if r == nil || r.Attributes["gen_ai.request.model"] != "whisper-1" || r.Attributes["traceai.audio.input_bytes"] != int64(10000) || r.Attributes["traceai.wire.truncated"] != nil {
			t.Fatalf("compressed core multipart lost for %s: %v", host, r)
		}
	}
}
