package traceaigenai_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/genai"
)

const testGeminiKey = "AIzaTEST-gemini-key-do-not-export-0001"

// fakeGemini serves the testdata fixtures in the Gemini REST shapes. The model
// in the URL picks the fixture, so one server covers every case.
type fakeGemini struct {
	t        *testing.T
	mu       sync.Mutex
	bodies   [][]byte
	headers  []http.Header
	paths    []string
	attempts map[string]int
}

func fixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func (f *fakeGemini) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	path := r.URL.Path
	model := path[strings.LastIndex(path, "/")+1:]
	model, method, _ := strings.Cut(model, ":")
	f.mu.Lock()
	f.bodies = append(f.bodies, body)
	f.headers = append(f.headers, r.Header.Clone())
	f.paths = append(f.paths, path)
	if f.attempts == nil {
		f.attempts = map[string]int{}
	}
	f.attempts[model]++
	attempt := f.attempts[model]
	f.mu.Unlock()

	jsonReply := func(status int, name string) {
		w.Header().Set("Content-Type", "application/json; charset=UTF-8")
		w.WriteHeader(status)
		_, _ = w.Write(fixture(f.t, name))
	}
	sseReply := func(name string) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		// Write event by event so the client really reads a stream.
		for _, ev := range strings.SplitAfter(string(fixture(f.t, name)), "\r\n\r\n") {
			if ev == "" {
				continue
			}
			_, _ = io.WriteString(w, ev)
			w.(http.Flusher).Flush()
		}
	}

	switch {
	case method == "countTokens":
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"totalTokens":3}`)
	case model == "fixture-404":
		jsonReply(http.StatusNotFound, "error_404.json")
	case model == "fixture-400-key":
		jsonReply(http.StatusBadRequest, "error_400_key.json")
	case model == "fixture-echo-key":
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = io.WriteString(w, `{"error":{"code":403,"message":"key `+r.Header.Get("x-goog-api-key")+` is blocked","status":"PERMISSION_DENIED"}}`)
	case model == "fixture-503-then-ok" && attempt == 1:
		jsonReply(http.StatusServiceUnavailable, "error_503.json")
	case model == "fixture-hang":
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		first, _, _ := strings.Cut(string(fixture(f.t, "stream_text.sse")), "\r\n\r\n")
		_, _ = io.WriteString(w, first+"\r\n\r\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	case method == "streamGenerateContent":
		name := "stream_text.sse"
		switch model {
		case "fixture-nousage":
			name = "stream_no_usage.sse"
		case "fixture-tool":
			name = "stream_function_call.sse"
		case "fixture-stream-error":
			name = "stream_error.sse"
		}
		sseReply(name)
	default:
		name := "generate_text.json"
		switch model {
		case "fixture-nousage":
			name = "generate_no_usage.json"
		case "fixture-tool":
			name = "generate_function_call.json"
		}
		jsonReply(http.StatusOK, name)
	}
}

type harness struct {
	t      *testing.T
	fake   *fakeGemini
	srv    *httptest.Server
	rec    *tracetest.SpanRecorder
	tp     *sdktrace.TracerProvider
	client *genai.Client
}

func newHarness(t *testing.T, opts ...traceaigenai.Option) *harness {
	t.Helper()
	h := &harness{t: t, fake: &fakeGemini{t: t}, rec: tracetest.NewSpanRecorder()}
	h.srv = httptest.NewServer(h.fake)
	t.Cleanup(h.srv.Close)
	h.tp = sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(h.rec))
	h.client = h.newClient(traceaigenai.NewHTTPClient(append([]traceaigenai.Option{traceaigenai.WithTracerProvider(h.tp)}, opts...)...), nil)
	return h
}

func (h *harness) newClient(hc *http.Client, retry *genai.HTTPRetryOptions) *genai.Client {
	h.t.Helper()
	c, err := genai.NewClient(context.Background(), &genai.ClientConfig{
		APIKey:      testGeminiKey,
		Backend:     genai.BackendGeminiAPI,
		HTTPClient:  hc,
		HTTPOptions: genai.HTTPOptions{BaseURL: h.srv.URL + "/", RetryOptions: retry},
	})
	if err != nil {
		h.t.Fatal(err)
	}
	return c
}

func (h *harness) spans() []sdktrace.ReadOnlySpan { return h.rec.Ended() }

func (h *harness) only() sdktrace.ReadOnlySpan {
	h.t.Helper()
	s := h.spans()
	if len(s) != 1 {
		h.t.Fatalf("got %d spans, want 1", len(s))
	}
	return s[0]
}

func attrs(s sdktrace.ReadOnlySpan) map[string]string {
	m := map[string]string{}
	for _, kv := range s.Attributes() {
		m[string(kv.Key)] = kv.Value.Emit()
	}
	return m
}

// everything returns every string the span would export: name, attributes,
// status, events and links.
func everything(s sdktrace.ReadOnlySpan) string {
	var b strings.Builder
	b.WriteString(s.Name() + "\n" + s.Status().Description + "\n")
	for _, kv := range s.Attributes() {
		b.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
	}
	for _, e := range s.Events() {
		b.WriteString(e.Name + "\n")
		for _, kv := range e.Attributes {
			b.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
		}
	}
	for _, kv := range s.Resource().Attributes() {
		b.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
	}
	return b.String()
}

func clearHideEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{"FI_HIDE_LLM_INVOCATION_PARAMETERS", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS",
		"FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_IMAGES", "FI_HIDE_INPUT_TEXT",
		"FI_HIDE_OUTPUT_TEXT", "FI_BASE64_IMAGE_MAX_LENGTH"} {
		t.Setenv(k, "")
	}
}

func weatherConfig() *genai.GenerateContentConfig {
	return &genai.GenerateContentConfig{
		SystemInstruction: genai.NewContentFromText("You are a weather assistant.", genai.RoleUser),
		Temperature:       genai.Ptr[float32](0.2),
		TopP:              genai.Ptr[float32](0.9),
		MaxOutputTokens:   256,
		StopSequences:     []string{"END"},
		Tools: []*genai.Tool{{FunctionDeclarations: []*genai.FunctionDeclaration{{
			Name:        "get_weather",
			Description: "Current weather for a city",
			Parameters: &genai.Schema{
				Type:       genai.TypeObject,
				Properties: map[string]*genai.Schema{"city": {Type: genai.TypeString}},
				Required:   []string{"city"},
			},
		}}}},
	}
}

// AC-03, AC-04, AC-05: the hook is ClientConfig.HTTPClient; one
// GenerateContent call is one LLM client span named after the model argument,
// with the request and response attributes traceai-google-genai writes.
func TestGenerateContentIsOneLLMSpan(t *testing.T) {
	clearHideEnv(t)
	h := newHarness(t)
	resp, err := h.client.Models.GenerateContent(context.Background(), "fixture-text",
		genai.Text("Name one primary color."), weatherConfig())
	if err != nil {
		t.Fatal(err)
	}
	if resp.Text() != "Red" {
		t.Fatalf("the SDK must still get the response, got %q", resp.Text())
	}
	s := h.only()
	a := attrs(s)
	if s.Name() != "generate_content fixture-text" || s.SpanKind() != trace.SpanKindClient {
		t.Errorf("name %q kind %v", s.Name(), s.SpanKind())
	}
	if s.Status().Code == codes.Error {
		t.Errorf("status %v", s.Status())
	}
	want := map[string]string{
		"gen_ai.span.kind":                         "LLM",
		"gen_ai.operation.name":                    "generate_content",
		"gen_ai.provider.name":                     "google",
		"gen_ai.request.model":                     "fixture-text",
		"gen_ai.request.max_tokens":                "256",
		"gen_ai.request.stop_sequences":            `["END"]`,
		"gen_ai.response.model":                    "gemini-3.8-flash",
		"gen_ai.response.id":                       "kBTSavOhL6jV-8YPt4GzgQE",
		"gen_ai.response.finish_reasons":           `["STOP"]`,
		"gen_ai.usage.input_tokens":                "13",
		"gen_ai.usage.output_tokens":               "103", // candidates 1 + thoughts 102, as in Python
		"gen_ai.usage.total_tokens":                "116",
		"gen_ai.usage.output_tokens.reasoning":     "102",
		"gen_ai.input.messages.0.message.role":     "system",
		"gen_ai.input.messages.0.message.content":  "You are a weather assistant.",
		"gen_ai.input.messages.1.message.role":     "user",
		"gen_ai.input.messages.1.message.content":  "Name one primary color.",
		"gen_ai.output.messages.0.message.role":    "model",
		"gen_ai.output.messages.0.message.content": "Red",
		"output.value":                             "Red",
		"output.mime_type":                         "text/plain",
		"input.mime_type":                          "application/json",
		"http.response.status_code":                "200",
	}
	for k, v := range want {
		if a[k] != v {
			t.Errorf("%s = %q, want %q", k, a[k], v)
		}
	}
	if !strings.HasPrefix(a["gen_ai.request.temperature"], "0.2") || !strings.HasPrefix(a["gen_ai.request.top_p"], "0.9") {
		t.Errorf("temperature/top_p = %q / %q", a["gen_ai.request.temperature"], a["gen_ai.request.top_p"])
	}
	if !strings.Contains(a["gen_ai.tool.definitions.0.tool.json_schema"], `"name":"get_weather"`) {
		t.Errorf("tool definition = %q", a["gen_ai.tool.definitions.0.tool.json_schema"])
	}
	if !strings.Contains(a["gen_ai.request.parameters"], `"maxOutputTokens":256`) {
		t.Errorf("parameters = %q", a["gen_ai.request.parameters"])
	}
	if !strings.Contains(a["input.value"], "Name one primary color.") {
		t.Errorf("input.value = %q", a["input.value"])
	}
	if strings.Contains(everything(s), "c2lnbmF0dXJl") {
		t.Error("thoughtSignature must never be copied")
	}
}

// AC-06: no usage attribute is written when the response has no usageMetadata,
// for a plain call and for a stream.
func TestUsageOmittedWhenAbsent(t *testing.T) {
	h := newHarness(t)
	if _, err := h.client.Models.GenerateContent(context.Background(), "fixture-nousage", genai.Text("hi"), nil); err != nil {
		t.Fatal(err)
	}
	for _, err := range h.client.Models.GenerateContentStream(context.Background(), "fixture-nousage", genai.Text("hi"), nil) {
		if err != nil {
			t.Fatal(err)
		}
	}
	spans := h.spans()
	if len(spans) != 2 {
		t.Fatalf("spans = %d", len(spans))
	}
	for _, s := range spans {
		for k, v := range attrs(s) {
			if strings.HasPrefix(k, "gen_ai.usage.") {
				t.Errorf("%s: %s = %s written without usage in the response", s.Name(), k, v)
			}
		}
	}
}

// AC-07: a multi-chunk stream is one span, ended only after the iterator is
// done, with the text joined and the usage of the last chunk (not summed).
func TestStreamIsOneSpanEndedAfterIterator(t *testing.T) {
	clearHideEnv(t)
	h := newHarness(t)
	var got strings.Builder
	chunks := 0
	for resp, err := range h.client.Models.GenerateContentStream(context.Background(), "fixture-text",
		genai.Text("Write three short sentences about Paris weather."), nil) {
		if err != nil {
			t.Fatal(err)
		}
		chunks++
		if chunks == 1 && len(h.spans()) != 0 {
			t.Fatal("span ended before the stream was read")
		}
		got.WriteString(resp.Text())
	}
	if chunks != 3 {
		t.Fatalf("chunks = %d, want 3", chunks)
	}
	s := h.only()
	a := attrs(s)
	if s.Name() != "generate_content fixture-text" || a["gen_ai.request.model"] != "fixture-text" {
		t.Errorf("name %q model %q", s.Name(), a["gen_ai.request.model"])
	}
	if a["output.value"] != got.String() || a["gen_ai.output.messages.0.message.content"] != got.String() {
		t.Errorf("streamed text not joined: %q vs %q", a["output.value"], got.String())
	}
	if a["gen_ai.usage.input_tokens"] != "9" || a["gen_ai.usage.output_tokens"] != "368" || a["gen_ai.usage.total_tokens"] != "377" {
		t.Errorf("usage = %s/%s/%s, want the last chunk's 9/368/377", a["gen_ai.usage.input_tokens"],
			a["gen_ai.usage.output_tokens"], a["gen_ai.usage.total_tokens"])
	}
	if a["gen_ai.response.finish_reasons"] != `["STOP"]` || s.Status().Code == codes.Error {
		t.Errorf("finish %q status %v", a["gen_ai.response.finish_reasons"], s.Status())
	}
}

// AC-08: cancelling the context mid-stream ends the span with ERROR and the
// call returns.
func TestCancelledStreamEndsError(t *testing.T) {
	h := newHarness(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		var last error
		for _, err := range h.client.Models.GenerateContentStream(ctx, "fixture-hang", genai.Text("hi"), nil) {
			if err != nil {
				last = err
				break
			}
			cancel()
		}
		done <- last
	}()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("the stream must surface the cancellation")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("cancelled stream did not return")
	}
	s := h.only()
	if s.Status().Code != codes.Error || attrs(s)["error.type"] != "canceled" {
		t.Fatalf("status %v error.type %q", s.Status(), attrs(s)["error.type"])
	}
	if strings.Contains(s.Status().Description, "127.0.0.1") {
		t.Error("status leaks the address")
	}
}

// A caller that breaks out of the range loop early ends the span without an
// error: the SDK closes the body.
func TestStreamStoppedEarlyEndsOK(t *testing.T) {
	h := newHarness(t)
	for _, err := range h.client.Models.GenerateContentStream(context.Background(), "fixture-text", genai.Text("hi"), nil) {
		if err != nil {
			t.Fatal(err)
		}
		break
	}
	s := h.only()
	if s.Status().Code == codes.Error {
		t.Fatalf("early stop is not an error: %v", s.Status())
	}
	if !strings.HasPrefix(attrs(s)["output.value"], "Paris experiences") {
		t.Errorf("partial output = %q", attrs(s)["output.value"])
	}
}

// A non-"data:" error block inside a stream (the shape the SDK's iterator
// reads as an error) ends the span with ERROR and the status token.
func TestStreamErrorBlockEndsError(t *testing.T) {
	clearHideEnv(t)
	h := newHarness(t)
	var sawErr bool
	for _, err := range h.client.Models.GenerateContentStream(context.Background(), "fixture-stream-error", genai.Text("hi"), nil) {
		if err != nil {
			sawErr = true
			break
		}
	}
	if !sawErr {
		t.Fatal("the SDK did not surface the stream error")
	}
	s := h.only()
	a := attrs(s)
	if s.Status().Code != codes.Error || a["error.type"] != "INTERNAL" ||
		s.Status().Description != "error in stream INTERNAL: Internal error encountered." {
		t.Fatalf("status %v error.type %q", s.Status(), a["error.type"])
	}
}

// AC-09: a function call in the response is recorded by name (and, with
// content capture on, its arguments); running the function is not spanned.
func TestFunctionCallRecorded(t *testing.T) {
	clearHideEnv(t)
	for _, stream := range []bool{false, true} {
		h := newHarness(t)
		if stream {
			for _, err := range h.client.Models.GenerateContentStream(context.Background(), "fixture-tool",
				genai.Text("What is the weather in Paris?"), weatherConfig()) {
				if err != nil {
					t.Fatal(err)
				}
			}
		} else {
			resp, err := h.client.Models.GenerateContent(context.Background(), "fixture-tool",
				genai.Text("What is the weather in Paris?"), weatherConfig())
			if err != nil {
				t.Fatal(err)
			}
			if fc := resp.FunctionCalls(); len(fc) != 1 || fc[0].Name != "get_weather" {
				t.Fatalf("SDK function calls = %v", fc)
			}
		}
		s := h.only()
		a := attrs(s)
		p := "gen_ai.output.messages.0.message.tool_calls.0."
		if a[p+"tool_call.function.name"] != "get_weather" || a[p+"tool_call.function.arguments"] != `{"city":"Paris"}` {
			t.Errorf("stream=%v tool call = %v", stream, a)
		}
		if a[p+"tool_call.id"] == "" {
			t.Errorf("stream=%v tool call id missing", stream)
		}
		if a["output.mime_type"] != "application/json" || !strings.Contains(a["output.value"], "get_weather") {
			t.Errorf("stream=%v output.value = %q", stream, a["output.value"])
		}
	}
}

// The second turn of a tool loop: the model's function call and the
// function response are input messages in the layout traceai-google-genai
// uses (tool_calls on the model turn, content + tool_call_id on the response).
func TestFunctionResponseTurnIsRecorded(t *testing.T) {
	clearHideEnv(t)
	h := newHarness(t)
	contents := []*genai.Content{
		genai.NewContentFromText("What is the weather in Paris?", genai.RoleUser),
		genai.NewContentFromParts([]*genai.Part{{FunctionCall: &genai.FunctionCall{ID: "call_1", Name: "get_weather", Args: map[string]any{"city": "Paris"}}}}, genai.RoleModel),
		genai.NewContentFromParts([]*genai.Part{{FunctionResponse: &genai.FunctionResponse{ID: "call_1", Name: "get_weather", Response: map[string]any{"temperature_c": 21}}}}, genai.RoleUser),
	}
	if _, err := h.client.Models.GenerateContent(context.Background(), "fixture-text", contents, nil); err != nil {
		t.Fatal(err)
	}
	a := attrs(h.only())
	for k, v := range map[string]string{
		"gen_ai.input.messages.1.message.role":                                      "model",
		"gen_ai.input.messages.1.message.tool_calls.0.tool_call.function.name":      "get_weather",
		"gen_ai.input.messages.1.message.tool_calls.0.tool_call.function.arguments": `{"city":"Paris"}`,
		"gen_ai.input.messages.2.message.content":                                   `{"temperature_c":21}`,
		"gen_ai.input.messages.2.message.tool_call_id":                              "call_1",
		"gen_ai.input.messages.2.message.name":                                      "get_weather",
	} {
		if a[k] != v {
			t.Errorf("%s = %q, want %q", k, a[k], v)
		}
	}
}

// AC-10, AC-13: with the traceAI hide switches on, no prompt, answer,
// system instruction, function argument or result leaves the process; model,
// usage and the function name in the tool definition survive. The Gemini API
// key is never an attribute in any mode.
func TestHideSwitchesRemoveContentAndKeyNeverExported(t *testing.T) {
	const marker = "MARKER-th8273-privacy"
	contents := []*genai.Content{
		genai.NewContentFromText("Weather in Paris? ref "+marker, genai.RoleUser),
		genai.NewContentFromParts([]*genai.Part{{FunctionCall: &genai.FunctionCall{Name: "get_weather", Args: map[string]any{"city": marker}}}}, genai.RoleModel),
		genai.NewContentFromParts([]*genai.Part{{FunctionResponse: &genai.FunctionResponse{Name: "get_weather", Response: map[string]any{"note": marker}}}}, genai.RoleUser),
	}
	cfg := weatherConfig()
	cfg.SystemInstruction = genai.NewContentFromText("system "+marker, genai.RoleUser)

	// Defaults: content is captured (traceAI convention), the key is not.
	clearHideEnv(t)
	h := newHarness(t)
	if _, err := h.client.Models.GenerateContent(context.Background(), "fixture-text", contents, cfg); err != nil {
		t.Fatal(err)
	}
	all := everything(h.only())
	if !strings.Contains(all, marker) {
		t.Fatal("default config must capture content")
	}
	if strings.Contains(all, testGeminiKey) {
		t.Fatal("API key exported")
	}

	// FI_HIDE_INPUTS + FI_HIDE_OUTPUTS from the environment.
	clearHideEnv(t)
	t.Setenv("FI_HIDE_INPUTS", "true")
	t.Setenv("FI_HIDE_OUTPUTS", "true")
	h = newHarness(t)
	if _, err := h.client.Models.GenerateContent(context.Background(), "fixture-text", contents, cfg); err != nil {
		t.Fatal(err)
	}
	for _, err := range h.client.Models.GenerateContentStream(context.Background(), "fixture-tool", contents, cfg) {
		if err != nil {
			t.Fatal(err)
		}
	}
	for _, s := range h.spans() {
		all := everything(s)
		for _, bad := range []string{marker, "Red", "Paris experiences", testGeminiKey, `{"city":"Paris"}`} {
			if strings.Contains(all, bad) {
				t.Errorf("%s exported %q with FI_HIDE_INPUTS/FI_HIDE_OUTPUTS", s.Name(), bad)
			}
		}
		a := attrs(s)
		if a["input.value"] != traceaigenai.RedactedValue || a["output.value"] != traceaigenai.RedactedValue {
			t.Errorf("input/output value = %q / %q", a["input.value"], a["output.value"])
		}
		if a["gen_ai.request.model"] == "" || a["gen_ai.usage.input_tokens"] == "" ||
			!strings.Contains(a["gen_ai.tool.definitions.0.tool.json_schema"], "get_weather") {
			t.Errorf("non-content attributes must survive: %v", a)
		}
	}

	// The same through WithTraceConfig, which wins over the environment.
	clearHideEnv(t)
	h = newHarness(t, traceaigenai.WithTraceConfig(traceaigenai.TraceConfig{HideInputs: true, HideOutputs: true}))
	if _, err := h.client.Models.GenerateContent(context.Background(), "fixture-text", contents, cfg); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(everything(h.only()), marker) {
		t.Error("WithTraceConfig did not hide content")
	}
}

// HTTP errors end the span with ERROR, the status code and the Google status
// token; the API message only while content is captured. The SDK still gets
// its APIError unchanged.
func TestHTTPErrorEndsError(t *testing.T) {
	clearHideEnv(t)
	h := newHarness(t)
	_, err := h.client.Models.GenerateContent(context.Background(), "fixture-404", genai.Text("hi"), nil)
	var apiErr genai.APIError
	if !errors.As(err, &apiErr) || apiErr.Code != 404 || apiErr.Status != "NOT_FOUND" {
		t.Fatalf("SDK error = %v", err)
	}
	s := h.only()
	a := attrs(s)
	if s.Status().Code != codes.Error || !strings.HasPrefix(s.Status().Description, "HTTP 404 NOT_FOUND: models/no-such-model-th8273 is not found") {
		t.Errorf("status = %v", s.Status())
	}
	if a["error.type"] != "NOT_FOUND" || a["http.response.status_code"] != "404" || a["error.message"] == "" {
		t.Errorf("attrs = %v", a)
	}

	t.Setenv("FI_HIDE_INPUTS", "true")
	h = newHarness(t)
	_, _ = h.client.Models.GenerateContent(context.Background(), "fixture-400-key", genai.Text("hi"), nil)
	s = h.only()
	if s.Status().Description != "HTTP 400 INVALID_ARGUMENT" || attrs(s)["error.message"] != "" {
		t.Errorf("hidden: status %q message %q", s.Status().Description, attrs(s)["error.message"])
	}
}

// AC-13: an API error that quotes the key is scrubbed before it reaches the
// span.
func TestAPIKeyScrubbedFromErrorText(t *testing.T) {
	clearHideEnv(t)
	h := newHarness(t)
	_, _ = h.client.Models.GenerateContent(context.Background(), "fixture-echo-key", genai.Text("hi"), nil)
	s := h.only()
	if strings.Contains(everything(s), testGeminiKey) {
		t.Fatal("API key from the error text reached the span")
	}
	if !strings.Contains(s.Status().Description, "key "+traceaigenai.RedactedValue+" is blocked") {
		t.Errorf("status = %q", s.Status().Description)
	}
}

// A transport error is recorded as a class, never as error text (which holds
// URLs and host names).
func TestTransportErrorHasNoHostOrURL(t *testing.T) {
	h := newHarness(t)
	h.srv.Close()
	_, err := h.client.Models.GenerateContent(context.Background(), "fixture-text", genai.Text("hi"), nil)
	if err == nil {
		t.Fatal("want an error from a closed server")
	}
	s := h.only()
	all := everything(s)
	if s.Status().Code != codes.Error || attrs(s)["error.type"] != "transport" {
		t.Errorf("status %v type %q", s.Status(), attrs(s)["error.type"])
	}
	for _, bad := range []string{"127.0.0.1", "dial tcp", "connection refused", "http://"} {
		if strings.Contains(all, bad) {
			t.Errorf("span carries %q", bad)
		}
	}
}

// The request the server sees is byte-identical with and without the
// transport, and the Future AGI keys never go to Gemini.
func TestRequestUnchanged(t *testing.T) {
	h := newHarness(t)
	plain := h.newClient(&http.Client{}, nil)
	for _, c := range []*genai.Client{plain, h.client} {
		if _, err := c.Models.GenerateContent(context.Background(), "fixture-text", genai.Text("same request"), weatherConfig()); err != nil {
			t.Fatal(err)
		}
	}
	h.fake.mu.Lock()
	defer h.fake.mu.Unlock()
	if len(h.fake.bodies) != 2 || string(h.fake.bodies[0]) != string(h.fake.bodies[1]) {
		t.Fatalf("bodies differ:\n%s\n%s", h.fake.bodies[0], h.fake.bodies[1])
	}
	for _, hd := range h.fake.headers {
		if hd.Get("X-Api-Key") != "" || hd.Get("X-Secret-Key") != "" || hd.Get("Traceparent") != "" {
			t.Errorf("unexpected header sent to Gemini: %v", hd)
		}
		if hd.Get("x-goog-api-key") != testGeminiKey {
			t.Error("the SDK's own key header must pass through")
		}
	}
}

// Requests other than generate calls pass through without a span.
func TestOtherEndpointsAreNotSpanned(t *testing.T) {
	h := newHarness(t)
	if _, err := h.client.Models.CountTokens(context.Background(), "fixture-text", genai.Text("hi"), nil); err != nil {
		t.Fatal(err)
	}
	if n := len(h.spans()); n != 0 {
		t.Fatalf("countTokens produced %d spans", n)
	}
}

// Chats are built on GenerateContent, so a chat turn is spanned too, with the
// history as input messages.
func TestChatSendMessageIsSpanned(t *testing.T) {
	clearHideEnv(t)
	h := newHarness(t)
	chat, err := h.client.Chats.Create(context.Background(), "fixture-text", nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, msg := range []string{"first", "second"} {
		if _, err := chat.SendMessage(context.Background(), genai.Part{Text: msg}); err != nil {
			t.Fatal(err)
		}
	}
	spans := h.spans()
	if len(spans) != 2 {
		t.Fatalf("spans = %d", len(spans))
	}
	a := attrs(spans[1])
	if a["gen_ai.input.messages.0.message.content"] != "first" || a["gen_ai.input.messages.1.message.role"] != "model" ||
		a["gen_ai.input.messages.2.message.content"] != "second" {
		t.Errorf("chat history = %v", a)
	}
}

// Session and user ids come from the context only; spans started inside the
// caller's span join its trace.
func TestSessionUserAndParentFromContext(t *testing.T) {
	h := newHarness(t)
	ctx := traceaigenai.ContextWithUserID(traceaigenai.ContextWithSessionID(context.Background(), "sess-1"), "user-7")
	ctx, parent := h.tp.Tracer("app").Start(ctx, "agent")
	if _, err := h.client.Models.GenerateContent(ctx, "fixture-text", genai.Text("hi"), nil); err != nil {
		t.Fatal(err)
	}
	parent.End()
	if _, err := h.client.Models.GenerateContent(context.Background(), "fixture-text", genai.Text("hi"), nil); err != nil {
		t.Fatal(err)
	}
	spans := h.spans()
	if len(spans) != 3 {
		t.Fatalf("spans = %d", len(spans))
	}
	llm, bare := spans[0], spans[2]
	if llm.Parent().SpanID() != parent.SpanContext().SpanID() || llm.SpanContext().TraceID() != parent.SpanContext().TraceID() {
		t.Error("LLM span is not a child of the caller's span")
	}
	if a := attrs(llm); a["session.id"] != "sess-1" || a["user.id"] != "user-7" {
		t.Errorf("ids = %v", a)
	}
	if a := attrs(bare); a["session.id"] != "" || a["user.id"] != "" {
		t.Error("a bare context must not get ids")
	}
}

// With HTTPOptions.RetryOptions the SDK retries at the HTTP layer; each
// attempt is its own LLM span, siblings under the caller's span, the failed
// one ERROR. Without RetryOptions the SDK does not retry.
func TestRetriesAreOneSpanPerAttempt(t *testing.T) {
	h := newHarness(t)
	retrying := h.newClient(traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(h.tp)),
		&genai.HTTPRetryOptions{Attempts: genai.Ptr[int32](2), InitialDelay: genai.Ptr[float64](0.01), MaxDelay: genai.Ptr[float64](0.01)})
	ctx, parent := h.tp.Tracer("app").Start(context.Background(), "agent")
	if _, err := retrying.Models.GenerateContent(ctx, "fixture-503-then-ok", genai.Text("hi"), nil); err != nil {
		t.Fatal(err)
	}
	parent.End()
	spans := h.spans()
	if len(spans) != 3 {
		t.Fatalf("spans = %d, want 2 attempts + parent", len(spans))
	}
	if spans[0].Status().Code != codes.Error || attrs(spans[0])["error.type"] != "UNAVAILABLE" || spans[1].Status().Code == codes.Error {
		t.Errorf("attempt statuses %v / %v", spans[0].Status(), spans[1].Status())
	}
	for _, s := range spans[:2] {
		if s.Parent().SpanID() != parent.SpanContext().SpanID() {
			t.Error("attempt is not under the caller's span")
		}
	}
}

// Without WithTracerProvider the global provider is used.
func TestGlobalProviderIsTheDefault(t *testing.T) {
	rec := tracetest.NewSpanRecorder()
	prev := otel.GetTracerProvider()
	otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec)))
	t.Cleanup(func() { otel.SetTracerProvider(prev) })
	h := newHarness(t)
	c := h.newClient(traceaigenai.NewHTTPClient(), nil)
	if _, err := c.Models.GenerateContent(context.Background(), "fixture-text", genai.Text("hi"), nil); err != nil {
		t.Fatal(err)
	}
	if n := len(rec.Ended()); n != 1 {
		t.Fatalf("global provider got %d spans", n)
	}
	if len(h.spans()) != 0 {
		t.Error("harness provider must not be used")
	}
}

// WrapClient keeps the caller's client settings and leaves it unchanged.
func TestWrapClientCopies(t *testing.T) {
	h := newHarness(t)
	base := &http.Client{Timeout: 7 * time.Second}
	wrapped := traceaigenai.WrapClient(base, traceaigenai.WithTracerProvider(h.tp))
	if base.Transport != nil || wrapped.Timeout != 7*time.Second || wrapped == base {
		t.Fatal("WrapClient must copy the client and keep its settings")
	}
	c := h.newClient(wrapped, nil)
	if _, err := c.Models.GenerateContent(context.Background(), "fixture-text", genai.Text("hi"), nil); err != nil {
		t.Fatal(err)
	}
	if len(h.spans()) != 1 {
		t.Fatal("wrapped client did not trace")
	}
	if traceaigenai.WrapClient(nil).Transport == nil {
		t.Fatal("nil client")
	}
}

// AC-01, AC-02, R-16: go.mod targets google.golang.org/genai v1.71.0 or later,
// never the retired module, and carries no replace directive.
func TestGoModTargetsCurrentSDK(t *testing.T) {
	b, err := os.ReadFile("go.mod")
	if err != nil {
		t.Fatal(err)
	}
	mod := string(b)
	if !strings.Contains(mod, "google.golang.org/genai v1.7") {
		t.Error("go.mod must require google.golang.org/genai v1.7x")
	}
	for _, bad := range []string{"github.com/google/generative-ai-go", "replace ", "fiotel"} {
		if strings.Contains(mod, bad) {
			t.Errorf("go.mod contains %q", bad)
		}
	}
}
