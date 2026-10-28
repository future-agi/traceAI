package traceaigenai

import (
	"io"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

func blocks(chunks ...string) []string {
	var p blockParser
	var got []string
	on := func(b []byte) { got = append(got, string(b)) }
	for _, c := range chunks {
		p.feed([]byte(c), on)
	}
	p.flush(on)
	return got
}

// The block parser splits the body like the SDK's scanner: on "\n\n" or
// "\r\n\r\n", across reads, with a final unterminated block.
func TestBlockParserMatchesSDKScanner(t *testing.T) {
	got := blocks("data: {\"a\"", ":1}\r\n\r\nda", "ta: {\"b\":2}\n\n{\"error\":{}}\r", "\n\r\ndata: tail")
	want := []string{`data: {"a":1}`, `data: {"b":2}`, `{"error":{}}`, "data: tail"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestModelFromPath(t *testing.T) {
	for in, want := range map[string]string{
		"/v1beta/models/gemini-3.8-flash:generateContent":                                             "gemini-3.8-flash",
		"/v1beta/models/gemini-3.8-flash:streamGenerateContent":                                       "gemini-3.8-flash",
		"/v1beta/tunedModels/my-tune:generateContent":                                                 "tunedModels/my-tune",
		"/v1beta1/projects/p/locations/us-central1/publishers/google/models/gemini-x:generateContent": "gemini-x",
		"/proxy/v1beta/models/m:generateContent/":                                                     "m",
	} {
		if got := modelFromPath(in); got != want {
			t.Errorf("modelFromPath(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestGenerateMethodMatchesOnlyGenerateCalls(t *testing.T) {
	for path, want := range map[string]bool{
		"/v1beta/models/m:generateContent":       true,
		"/v1beta/models/m:streamGenerateContent": true,
		"/v1beta/models/m:countTokens":           false,
		"/v1beta/models/m:embedContent":          false,
		"/v1beta/models/m":                       false,
		"/v1beta/files":                          false,
	} {
		req, _ := http.NewRequest(http.MethodPost, "http://x"+path+"?alt=sse", nil)
		if _, ok := generateMethod(req); ok != want {
			t.Errorf("%s: %v", path, ok)
		}
	}
	req, _ := http.NewRequest(http.MethodGet, "http://x/v1beta/models/m:generateContent", nil)
	if _, ok := generateMethod(req); ok {
		t.Error("GET must not match")
	}
}

func TestParseAPIError(t *testing.T) {
	for in, want := range map[string]apiError{
		`{"error":{"code":404,"message":"m","status":"NOT_FOUND","details":[{"x":"y"}]}}`: {404, "m", "NOT_FOUND"},
		`{"error":"plain"}`: {Message: "plain"},
		`not json`:          {},
		`{"candidates":[]}`: {},
	} {
		if got := parseAPIError([]byte(in)); got != want {
			t.Errorf("parseAPIError(%s) = %+v, want %+v", in, got, want)
		}
	}
}

// Without GetBody the body is read once and a clone carries a replay; the
// caller's request is not modified (RoundTripper contract).
func TestRequestBodyWithoutGetBodyDoesNotModifyRequest(t *testing.T) {
	body := `{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}`
	req, _ := http.NewRequest(http.MethodPost, "http://x/v1beta/models/m:generateContent", io.NopCloser(strings.NewReader(body)))
	req.GetBody = nil
	origBody := req.Body
	got, out := requestBody(req)
	if string(got) != body {
		t.Fatalf("copy = %q", got)
	}
	if req.Body != origBody {
		t.Fatal("caller's request body was replaced")
	}
	if out == req {
		t.Fatal("a clone must carry the replayed body")
	}
	sent, _ := io.ReadAll(out.Body)
	if string(sent) != body {
		t.Fatalf("replayed body = %q", sent)
	}
}

// Input images are kept as data: URLs (and are subject to the image
// switches); audio and video bytes never are; generated images in a response
// keep only their type.
func TestMediaParts(t *testing.T) {
	req := `{"contents":[{"role":"user","parts":[
	 {"text":"look"},
	 {"inlineData":{"mimeType":"image/png","data":"iVBORw0KGgo="}},
	 {"inlineData":{"mimeType":"audio/wav","data":"UklGRiQAAABXQVZF"}},
	 {"fileData":{"mimeType":"video/mp4","fileUri":"https://generativelanguage.googleapis.com/v1beta/files/v1"}}]}]}`
	p := parseGenerateRequest([]byte(req), TraceConfig{})
	a := map[string]string{}
	for _, kv := range p.attrs {
		a[string(kv.Key)] = kv.Value.Emit()
	}
	pre := GenAIInputMessages + ".0." + MessageContents + "."
	if a[pre+"0."+MessageContentText] != "look" || a[pre+"1."+MessageContentImage] != "data:image/png;base64,iVBORw0KGgo=" ||
		a[pre+"2."+MessageContentType] != "audio" || a[pre+"3."+MessageContentType] != "video" {
		t.Fatalf("attrs = %v", a)
	}
	for k, v := range a {
		if strings.Contains(v, "UklGR") || strings.Contains(v, "files/v1") {
			t.Errorf("%s carries audio/video data: %q", k, v)
		}
	}

	acc := newAccumulator()
	acc.addResponse([]byte(`{"candidates":[{"content":{"role":"model","parts":[{"text":"here"},{"inlineData":{"mimeType":"image/png","data":"GENERATEDBYTES"}}]}}]}`))
	for _, kv := range acc.attributes() {
		if strings.Contains(kv.Value.Emit(), "GENERATEDBYTES") {
			t.Errorf("generated image bytes in %s", kv.Key)
		}
	}
}

// Thought summaries are not the answer; consecutive stream text parts are
// joined.
func TestThoughtsSkippedAndStreamTextJoined(t *testing.T) {
	acc := newAccumulator()
	acc.addResponse([]byte(`{"candidates":[{"content":{"role":"model","parts":[{"text":"thinking...","thought":true},{"text":"Hel"}]}}]}`))
	acc.addResponse([]byte(`{"candidates":[{"content":{"role":"model","parts":[{"text":"lo"},{"text":"","thoughtSignature":"c2ln"}]},"finishReason":"STOP"}]}`))
	a := map[string]string{}
	for _, kv := range acc.attributes() {
		a[string(kv.Key)] = kv.Value.Emit()
	}
	if a[OutputValue] != "Hello" || a[GenAIOutputMessages+".0."+MessageContent] != "Hello" {
		t.Fatalf("attrs = %v", a)
	}
	for _, v := range a {
		if strings.Contains(v, "thinking") || strings.Contains(v, "c2ln") {
			t.Errorf("thought content exported: %q", v)
		}
	}
}

func TestUsageMapping(t *testing.T) {
	acc := newAccumulator()
	acc.addResponse([]byte(`{"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":4,"totalTokenCount":20,"thoughtsTokenCount":6,"cachedContentTokenCount":3}}`))
	a := map[string]string{}
	for _, kv := range acc.attributes() {
		a[string(kv.Key)] = kv.Value.Emit()
	}
	for k, v := range map[string]string{
		GenAIUsageInputTokens: "10", GenAIUsageOutputTokens: "10", GenAIUsageTotalTokens: "20",
		GenAIUsageReasoningTokens: "6", GenAIUsageCacheReadTokens: "3",
	} {
		if a[k] != v {
			t.Errorf("%s = %q, want %q", k, a[k], v)
		}
	}
	acc = newAccumulator()
	acc.addResponse([]byte(`{"usageMetadata":{"promptTokenCount":5}}`))
	for _, kv := range acc.attributes() {
		if kv.Key == GenAIUsageOutputTokens || kv.Key == GenAIUsageTotalTokens {
			t.Errorf("%s written without a count in the response", kv.Key)
		}
	}
}
