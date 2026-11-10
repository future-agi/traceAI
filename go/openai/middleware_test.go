package traceaiopenai_test

import (
	"context"
	"encoding/json"
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

	traceaiopenai "github.com/future-agi/traceAI/go/openai"
	"github.com/openai/openai-go/v3"
	"github.com/openai/openai-go/v3/option"
	"github.com/openai/openai-go/v3/responses"
	"github.com/openai/openai-go/v3/shared"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

const testOpenAIKey = "sk-test-openai-key-do-not-export"

// fakeOpenAI serves the testdata fixtures. The request model picks the
// fixture, so one server covers every case.
type fakeOpenAI struct {
	t        *testing.T
	mu       sync.Mutex
	bodies   [][]byte
	headers  []http.Header
	paths    []string
	attempts int
	// gate, when set, holds a stream after its first event until closed.
	gate chan struct{}
}

func fixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func (f *fakeOpenAI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	f.mu.Lock()
	f.bodies = append(f.bodies, body)
	f.headers = append(f.headers, r.Header.Clone())
	f.paths = append(f.paths, r.URL.Path)
	f.attempts++
	attempt := f.attempts
	gate := f.gate
	f.mu.Unlock()

	var req struct {
		Model  string `json:"model"`
		Stream bool   `json:"stream"`
	}
	_ = json.Unmarshal(body, &req)

	switch {
	case strings.HasSuffix(r.URL.Path, "/embeddings"):
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"object":"list","data":[{"object":"embedding","index":0,"embedding":[0.1,0.2]}],"model":"text-embedding-3-small","usage":{"prompt_tokens":2,"total_tokens":2}}`)
	case strings.HasSuffix(r.URL.Path, "/responses"):
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"id":"resp_1","object":"response","created_at":1,"model":"gpt-4.1-mini","output":[],"status":"completed"}`)
	case req.Model == "fixture-404":
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write(fixture(f.t, "error_404.json"))
	case req.Model == "fixture-500-then-ok" && attempt == 1:
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(w, `{"error":{"message":"upstream overloaded","type":"server_error","code":null}}`)
	case req.Model == "fixture-hang":
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "data: {\"id\":\"chatcmpl-hang\",\"model\":\"gpt-4.1-mini\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"partial\"}}]}\n\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	case req.Stream:
		name := "chat_stream.sse"
		switch req.Model {
		case "fixture-nousage":
			name = "chat_stream_no_usage.sse"
		case "fixture-tool":
			name = "chat_stream_tool_calls.sse"
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		// Write event by event so the client really reads a stream.
		for _, ev := range strings.SplitAfter(string(fixture(f.t, name)), "\n\n") {
			if ev == "" {
				continue
			}
			_, _ = io.WriteString(w, ev)
			w.(http.Flusher).Flush()
			if gate != nil {
				<-gate
				gate = nil
			}
		}
	default:
		name := "chat_completion.json"
		switch req.Model {
		case "fixture-nousage":
			name = "chat_completion_no_usage.json"
		case "fixture-tool":
			name = "chat_completion_tool_call.json"
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(fixture(f.t, name))
	}
}

type harness struct {
	srv    *httptest.Server
	fake   *fakeOpenAI
	rec    *tracetest.SpanRecorder
	tp     *sdktrace.TracerProvider
	client openai.Client
}

func newHarness(t *testing.T, mwOpts []traceaiopenai.Option, clientOpts ...option.RequestOption) *harness {
	t.Helper()
	t.Setenv("OPENAI_BASE_URL", "")
	t.Setenv("OPENAI_API_KEY", "")
	h := &harness{fake: &fakeOpenAI{t: t}, rec: tracetest.NewSpanRecorder()}
	h.srv = httptest.NewServer(h.fake)
	t.Cleanup(h.srv.Close)
	h.tp = sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(h.rec))
	mwOpts = append([]traceaiopenai.Option{traceaiopenai.WithTracerProvider(h.tp)}, mwOpts...)
	opts := []option.RequestOption{
		option.WithBaseURL(h.srv.URL + "/v1/"),
		option.WithAPIKey(testOpenAIKey),
		option.WithMaxRetries(0),
		option.WithMiddleware(traceaiopenai.Middleware(mwOpts...)),
	}
	h.client = openai.NewClient(append(opts, clientOpts...)...)
	return h
}

const marker = "MARKER-7f3c-do-not-export"

func chatParams(model string) openai.ChatCompletionNewParams {
	return openai.ChatCompletionNewParams{
		Model: model,
		Messages: []openai.ChatCompletionMessageParamUnion{
			openai.SystemMessage("You are terse. " + marker),
			openai.UserMessage("What is the capital of France? " + marker),
		},
		Temperature:         openai.Float(0.2),
		MaxCompletionTokens: openai.Int(64),
		User:                openai.String("person@example.com"),
	}
}

func toolParams(model string) openai.ChatCompletionNewParams {
	p := chatParams(model)
	p.Tools = []openai.ChatCompletionToolUnionParam{
		openai.ChatCompletionFunctionTool(shared.FunctionDefinitionParam{
			Name:        "get_weather",
			Description: openai.String("Current weather for a city"),
			Parameters: shared.FunctionParameters{
				"type":       "object",
				"properties": map[string]any{"city": map[string]any{"type": "string"}},
				"required":   []string{"city"},
			},
		}),
	}
	return p
}

func attrs(s sdktrace.ReadOnlySpan) map[string]attribute.Value {
	m := map[string]attribute.Value{}
	for _, kv := range s.Attributes() {
		m[string(kv.Key)] = kv.Value
	}
	return m
}

func str(m map[string]attribute.Value, k string) string {
	v, ok := m[k]
	if !ok {
		return ""
	}
	return v.Emit()
}

func onlySpan(t *testing.T, rec *tracetest.SpanRecorder) sdktrace.ReadOnlySpan {
	t.Helper()
	ended := rec.Ended()
	if len(ended) != 1 {
		t.Fatalf("ended spans = %d, want exactly 1", len(ended))
	}
	if n := len(rec.Started()); n != 1 {
		t.Fatalf("started spans = %d, want exactly 1 (no orphan)", n)
	}
	return ended[0]
}

// allText returns every exported string of a span: name, status, attribute
// keys and values, event names and attributes.
func allText(s sdktrace.ReadOnlySpan) string {
	var b strings.Builder
	b.WriteString(s.Name() + "\n" + s.Status().Description + "\n")
	for _, kv := range s.Attributes() {
		b.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
	}
	for _, ev := range s.Events() {
		b.WriteString(ev.Name + "\n")
		for _, kv := range ev.Attributes {
			b.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
		}
	}
	for _, kv := range s.Resource().Attributes() {
		b.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
	}
	return b.String()
}

// AC-03, AC-04: one chat completion, one LLM client span with the request
// model, the response model, usage and the content (captured by default).
func TestChatCompletionIsOneSpan(t *testing.T) {
	h := newHarness(t, []traceaiopenai.Option{traceaiopenai.WithTraceConfig(traceaiopenai.TraceConfig{})})
	res, err := h.client.Chat.Completions.New(context.Background(), chatParams("gpt-4.1-mini"))
	if err != nil {
		t.Fatal(err)
	}
	if res.Choices[0].Message.Content != "Paris is the capital of France." {
		t.Fatalf("SDK got %q; the middleware must hand the body back intact", res.Choices[0].Message.Content)
	}
	s := onlySpan(t, h.rec)
	a := attrs(s)
	if s.Name() != "chat gpt-4.1-mini" || s.SpanKind() != trace.SpanKindClient {
		t.Errorf("name/kind = %q/%v", s.Name(), s.SpanKind())
	}
	want := map[string]string{
		traceaiopenai.GenAISpanKind:                "LLM",
		traceaiopenai.GenAIOperationName:           "chat",
		traceaiopenai.GenAIProviderName:            "openai",
		traceaiopenai.GenAIRequestModel:            "gpt-4.1-mini",
		traceaiopenai.GenAIResponseModel:           "gpt-4.1-mini-2025-04-14",
		traceaiopenai.GenAIResponseID:              "chatcmpl-fixture-basic",
		traceaiopenai.GenAIUsageInputTokens:        "21",
		traceaiopenai.GenAIUsageOutputTokens:       "8",
		traceaiopenai.GenAIUsageTotalTokens:        "29",
		traceaiopenai.GenAIRequestTemperature:      "0.2",
		traceaiopenai.GenAIRequestMaxTokens:        "64",
		traceaiopenai.HTTPResponseStatusCode:       "200",
		traceaiopenai.OutputValue:                  "Paris is the capital of France.",
		traceaiopenai.OutputMimeType:               "text/plain",
		"gen_ai.input.messages.0.message.role":     "system",
		"gen_ai.input.messages.1.message.role":     "user",
		"gen_ai.input.messages.1.message.content":  "What is the capital of France? " + marker,
		"gen_ai.output.messages.0.message.role":    "assistant",
		"gen_ai.output.messages.0.message.content": "Paris is the capital of France.",
	}
	for k, v := range want {
		if got := str(a, k); got != v {
			t.Errorf("%s = %q, want %q", k, got, v)
		}
	}
	if got := a[traceaiopenai.GenAIResponseFinishReasons].AsStringSlice(); len(got) != 1 || got[0] != "stop" {
		t.Errorf("finish reasons = %v", got)
	}
	if !strings.Contains(str(a, traceaiopenai.InputValue), marker) || str(a, traceaiopenai.InputMimeType) != "application/json" {
		t.Errorf("input.value = %q", str(a, traceaiopenai.InputValue))
	}
	params := str(a, traceaiopenai.GenAIRequestParameters)
	if !strings.Contains(params, `"temperature":0.2`) || strings.Contains(params, "example.com") || strings.Contains(params, "user") {
		t.Errorf("request parameters must be the allow list only: %s", params)
	}
	if _, ok := a[traceaiopenai.GenAIUsageCacheReadTokens]; ok {
		t.Error("cached_tokens 0 must not be written")
	}
	if s.Status().Code == codes.Error {
		t.Error("status must not be ERROR")
	}
	if _, ok := a["fi.span.kind"]; ok {
		t.Error("only gen_ai.span.kind types the span, as in traceai-openai")
	}
}

// The span is a child of the caller's span: no sibling or orphan trace.
func TestSpanIsChildOfCallerSpan(t *testing.T) {
	h := newHarness(t, nil)
	ctx, parent := h.tp.Tracer("app").Start(context.Background(), "agent")
	if _, err := h.client.Chat.Completions.New(ctx, chatParams("gpt-4.1-mini")); err != nil {
		t.Fatal(err)
	}
	parent.End()
	var llm sdktrace.ReadOnlySpan
	for _, s := range h.rec.Ended() {
		if s.Name() == "chat gpt-4.1-mini" {
			llm = s
		}
	}
	if llm == nil || llm.Parent().SpanID() != parent.SpanContext().SpanID() || llm.SpanContext().TraceID() != parent.SpanContext().TraceID() {
		t.Fatal("LLM span must be a child of the caller's span in the same trace")
	}
}

// AC-05: no usage in the response means no usage attributes, not zeros.
func TestUsageOmittedWhenAbsent(t *testing.T) {
	h := newHarness(t, nil)
	if _, err := h.client.Chat.Completions.New(context.Background(), chatParams("fixture-nousage")); err != nil {
		t.Fatal(err)
	}
	a := attrs(onlySpan(t, h.rec))
	for _, k := range []string{traceaiopenai.GenAIUsageInputTokens, traceaiopenai.GenAIUsageOutputTokens, traceaiopenai.GenAIUsageTotalTokens} {
		if _, ok := a[k]; ok {
			t.Errorf("%s written without usage in the response", k)
		}
	}
	if str(a, traceaiopenai.GenAIRequestModel) != "fixture-nousage" {
		t.Error("model must still be set")
	}
}

// AC-06: a three-chunk stream is one span, ended only after the stream is
// read to the end.
func TestStreamIsOneSpanEndedAfterTheStream(t *testing.T) {
	h := newHarness(t, nil)
	h.fake.gate = make(chan struct{})
	p := chatParams("gpt-4.1-mini")
	p.StreamOptions = openai.ChatCompletionStreamOptionsParam{IncludeUsage: openai.Bool(true)}
	stream := h.client.Chat.Completions.NewStreaming(context.Background(), p)
	var text strings.Builder
	chunks := 0
	for stream.Next() {
		chunks++
		if chunks == 1 {
			// The server is holding the rest of the stream: the span must
			// still be open.
			if n := len(h.rec.Ended()); n != 0 {
				t.Fatalf("span ended after the first chunk (%d ended)", n)
			}
			close(h.fake.gate)
		}
		for _, c := range stream.Current().Choices {
			text.WriteString(c.Delta.Content)
		}
	}
	if err := stream.Err(); err != nil {
		t.Fatal(err)
	}
	_ = stream.Close()
	if text.String() != "Hello there friend" {
		t.Fatalf("SDK read %q; the stream must pass through unchanged", text.String())
	}
	s := onlySpan(t, h.rec)
	a := attrs(s)
	if str(a, "gen_ai.output.messages.0.message.content") != "Hello there friend" || str(a, traceaiopenai.OutputValue) != "Hello there friend" {
		t.Errorf("accumulated output = %q", str(a, traceaiopenai.OutputValue))
	}
	if str(a, traceaiopenai.GenAIUsageInputTokens) != "11" || str(a, traceaiopenai.GenAIUsageOutputTokens) != "3" {
		t.Errorf("usage from the final chunk missing: %v", a)
	}
	if str(a, traceaiopenai.GenAIResponseID) != "chatcmpl-fixture-stream" || a[traceaiopenai.GenAIResponseFinishReasons].AsStringSlice()[0] != "stop" {
		t.Error("response id / finish reason missing")
	}
	if s.Status().Code == codes.Error {
		t.Error("completed stream must not be ERROR")
	}
}

func TestStreamWithoutUsageChunkOmitsUsage(t *testing.T) {
	h := newHarness(t, nil)
	stream := h.client.Chat.Completions.NewStreaming(context.Background(), chatParams("fixture-nousage"))
	for stream.Next() {
	}
	_ = stream.Close()
	a := attrs(onlySpan(t, h.rec))
	if _, ok := a[traceaiopenai.GenAIUsageInputTokens]; ok {
		t.Error("usage written although the stream had no usage chunk")
	}
}

// AC-07: cancelling the context mid-stream ends the span with ERROR and does
// not hang.
func TestStreamCancelEndsSpanWithError(t *testing.T) {
	h := newHarness(t, nil)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		stream := h.client.Chat.Completions.NewStreaming(ctx, chatParams("fixture-hang"))
		if stream.Next() {
			cancel()
		}
		for stream.Next() {
		}
		_ = stream.Close()
	}()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("cancelled stream hung")
	}
	cancel()
	s := onlySpan(t, h.rec)
	if s.Status().Code != codes.Error || str(attrs(s), traceaiopenai.ErrorType) != "canceled" {
		t.Fatalf("status = %v %q, error.type = %q", s.Status().Code, s.Status().Description, str(attrs(s), traceaiopenai.ErrorType))
	}
	if str(attrs(s), "gen_ai.output.messages.0.message.content") != "partial" {
		t.Error("content received before the cancel should be kept")
	}
}

// AC-08: the tool call the model returned is recorded, non-streaming and
// streaming (arguments split across chunks, two parallel calls).
func TestToolCallsRecorded(t *testing.T) {
	h := newHarness(t, nil)
	res, err := h.client.Chat.Completions.New(context.Background(), toolParams("fixture-tool"))
	if err != nil {
		t.Fatal(err)
	}
	if res.Choices[0].Message.ToolCalls[0].Function.Name != "get_weather" {
		t.Fatal("SDK did not get the tool call")
	}
	a := attrs(onlySpan(t, h.rec))
	p := "gen_ai.output.messages.0.message.tool_calls.0.tool_call."
	if str(a, p+"function.name") != "get_weather" || str(a, p+"id") != "call_fixture_1" || str(a, p+"function.arguments") != `{"city":"Paris"}` {
		t.Errorf("tool call attributes: %v", a)
	}
	if !strings.Contains(str(a, "gen_ai.tool.definitions.0.tool.json_schema"), `"get_weather"`) {
		t.Error("tool definition missing")
	}
	if a[traceaiopenai.GenAIResponseFinishReasons].AsStringSlice()[0] != "tool_calls" {
		t.Error("finish reason")
	}

	h2 := newHarness(t, nil)
	stream := h2.client.Chat.Completions.NewStreaming(context.Background(), toolParams("fixture-tool"))
	acc := openai.ChatCompletionAccumulator{}
	for stream.Next() {
		acc.AddChunk(stream.Current())
	}
	_ = stream.Close()
	a = attrs(onlySpan(t, h2.rec))
	if str(a, p+"function.name") != "get_weather" || str(a, p+"function.arguments") != `{"city":"Paris"}` || str(a, p+"id") != "call_s1" {
		t.Errorf("streamed tool call 0: %v", a)
	}
	p1 := "gen_ai.output.messages.0.message.tool_calls.1.tool_call."
	if str(a, p1+"function.name") != "get_time" || str(a, p1+"id") != "call_s2" {
		t.Errorf("streamed tool call 1: %v", a)
	}
}

// Privacy: with FI_HIDE_INPUTS and FI_HIDE_OUTPUTS on, no prompt, response,
// tool argument or error text leaves the process. Model and usage stay.
func TestHiddenContentNeverExported(t *testing.T) {
	cfg := traceaiopenai.TraceConfig{HideInputs: true, HideOutputs: true}
	for _, model := range []string{"gpt-4.1-mini", "fixture-tool", "fixture-404"} {
		for _, streaming := range []bool{false, true} {
			h := newHarness(t, []traceaiopenai.Option{traceaiopenai.WithTraceConfig(cfg)})
			p := toolParams(model)
			p.Messages = append(p.Messages, openai.ToolMessage(`{"temp":"21C `+marker+`"}`, "call_prev"))
			if streaming {
				s := h.client.Chat.Completions.NewStreaming(context.Background(), p)
				for s.Next() {
				}
				_ = s.Close()
			} else {
				_, _ = h.client.Chat.Completions.New(context.Background(), p)
			}
			s := onlySpan(t, h.rec)
			text := allText(s)
			for _, leak := range []string{marker, "Paris", "Hello", "does not exist", "capital"} {
				if strings.Contains(text, leak) {
					t.Errorf("model=%s stream=%v: %q exported with content hidden:\n%s", model, streaming, leak, text)
				}
			}
			a := attrs(s)
			if str(a, traceaiopenai.GenAIRequestModel) != model {
				t.Errorf("model must survive hiding")
			}
			if model == "gpt-4.1-mini" && !streaming && str(a, traceaiopenai.GenAIUsageInputTokens) != "21" {
				t.Error("usage must survive hiding")
			}
		}
	}
}

func TestHideSwitchesAreReadFromEnvironment(t *testing.T) {
	t.Setenv(traceaiopenai.EnvHideInputs, "true")
	t.Setenv(traceaiopenai.EnvHideOutputs, "true")
	h := newHarness(t, nil) // no WithTraceConfig: FI_HIDE_* are read
	if _, err := h.client.Chat.Completions.New(context.Background(), chatParams("gpt-4.1-mini")); err != nil {
		t.Fatal(err)
	}
	a := attrs(onlySpan(t, h.rec))
	if str(a, traceaiopenai.InputValue) != traceaiopenai.RedactedValue || str(a, traceaiopenai.OutputValue) != traceaiopenai.RedactedValue {
		t.Errorf("env switches ignored: %v", a)
	}
	if strings.Contains(allText(onlySpan(t, h.rec)), marker) {
		t.Error("marker exported")
	}
}

// J4: an API error is an ERROR span with the HTTP status and the error type;
// the API's message only when outputs are captured.
func TestHTTPErrorIsErrorSpan(t *testing.T) {
	h := newHarness(t, nil)
	_, err := h.client.Chat.Completions.New(context.Background(), chatParams("fixture-404"))
	var apiErr *openai.Error
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 404 || apiErr.Code != "model_not_found" {
		t.Fatalf("SDK error must be unchanged, got %v", err)
	}
	s := onlySpan(t, h.rec)
	a := attrs(s)
	if s.Status().Code != codes.Error || !strings.HasPrefix(s.Status().Description, "HTTP 404 model_not_found") {
		t.Errorf("status = %v %q", s.Status().Code, s.Status().Description)
	}
	if str(a, traceaiopenai.HTTPResponseStatusCode) != "404" || str(a, traceaiopenai.ErrorType) != "invalid_request_error" {
		t.Errorf("attrs = %v", a)
	}
	if !strings.Contains(str(a, traceaiopenai.ErrorMessage), "does not exist") {
		t.Error("error message expected when outputs are captured")
	}

	for _, cfg := range []traceaiopenai.TraceConfig{{HideOutputs: true}, {HideInputs: true}} {
		hidden := newHarness(t, []traceaiopenai.Option{traceaiopenai.WithTraceConfig(cfg)})
		_, _ = hidden.client.Chat.Completions.New(context.Background(), chatParams("fixture-404"))
		s = onlySpan(t, hidden.rec)
		if s.Status().Description != "HTTP 404 model_not_found" || strings.Contains(allText(s), "does not exist") {
			t.Errorf("%+v: error text exported: %q", cfg, allText(s))
		}
	}
}

func TestTransportErrorHasNoHostOrURL(t *testing.T) {
	h := newHarness(t, nil)
	h.srv.Close()
	_, err := h.client.Chat.Completions.New(context.Background(), chatParams("gpt-4.1-mini"))
	if err == nil {
		t.Fatal("expected a connection error")
	}
	s := onlySpan(t, h.rec)
	if s.Status().Code != codes.Error || str(attrs(s), traceaiopenai.ErrorType) != "transport" {
		t.Errorf("status = %v, error.type = %q", s.Status().Code, str(attrs(s), traceaiopenai.ErrorType))
	}
	if strings.Contains(allText(s), "127.0.0.1") || strings.Contains(allText(s), "dial") {
		t.Errorf("transport error text exported: %s", allText(s))
	}
}

// AC-12: no credential in any attribute, and the Future AGI keys never go to
// the model provider.
func TestNoCredentialsExported(t *testing.T) {
	h := newHarness(t, nil)
	_, _ = h.client.Chat.Completions.New(context.Background(), chatParams("gpt-4.1-mini"))
	text := allText(onlySpan(t, h.rec))
	for _, secret := range []string{testOpenAIKey, "Bearer", "Authorization"} {
		if strings.Contains(text, secret) {
			t.Errorf("%q exported", secret)
		}
	}
	hdr := h.fake.headers[0]
	if hdr.Get("Authorization") != "Bearer "+testOpenAIKey {
		t.Error("the SDK's own auth header must be untouched")
	}
	if hdr.Get("X-Api-Key") != "" || hdr.Get("X-Secret-Key") != "" {
		t.Error("Future AGI headers must not be sent to the model provider")
	}
}

// AC-13 and V-D: session.id and user.id come from the context and are never
// invented for a bare context.
func TestSessionFromContext(t *testing.T) {
	h := newHarness(t, nil)
	ctx := traceaiopenai.ContextWithUserID(traceaiopenai.ContextWithSessionID(context.Background(), "session-42"), "user-9")
	_, _ = h.client.Chat.Completions.New(ctx, chatParams("gpt-4.1-mini"))
	a := attrs(onlySpan(t, h.rec))
	if str(a, traceaiopenai.SessionID) != "session-42" || str(a, traceaiopenai.UserID) != "user-9" {
		t.Errorf("session/user = %q/%q", str(a, traceaiopenai.SessionID), str(a, traceaiopenai.UserID))
	}

	// A context derived from the session context (a caller span on top) still
	// carries it; a bare context does not.
	derived, parent := h.tp.Tracer("app").Start(ctx, "agent")
	_, _ = h.client.Chat.Completions.New(derived, chatParams("gpt-4.1-mini"))
	parent.End()
	_, _ = h.client.Chat.Completions.New(context.Background(), chatParams("gpt-4.1-mini"))
	var chats []sdktrace.ReadOnlySpan
	for _, s := range h.rec.Ended() {
		if strings.HasPrefix(s.Name(), "chat ") {
			chats = append(chats, s)
		}
	}
	if len(chats) != 3 || str(attrs(chats[1]), traceaiopenai.SessionID) != "session-42" {
		t.Fatal("a context derived from the session context must keep session.id")
	}
	if _, ok := attrs(chats[2])[traceaiopenai.SessionID]; ok {
		t.Error("bare context must not get a session id")
	}
	if _, ok := attrs(chats[2])[traceaiopenai.UserID]; ok {
		t.Error("bare context must not get a user id")
	}
}

// Only Chat Completions are spanned; other endpoints pass through untouched.
func TestOtherEndpointsPassThrough(t *testing.T) {
	h := newHarness(t, nil)
	if _, err := h.client.Embeddings.New(context.Background(), openai.EmbeddingNewParams{
		Model: openai.EmbeddingModelTextEmbedding3Small,
		Input: openai.EmbeddingNewParamsInputUnion{OfString: openai.String("hello")},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := h.client.Responses.New(context.Background(), responses.ResponseNewParams{
		Model: "gpt-4.1-mini",
		Input: responses.ResponseNewParamsInputUnion{OfString: openai.String("hello")},
	}); err != nil {
		t.Fatal(err)
	}
	if n := len(h.rec.Started()); n != 0 {
		t.Fatalf("%d spans for non-chat endpoints, want 0", n)
	}
}

// The request the provider receives is byte-for-byte what the SDK built.
func TestRequestBodyUnchanged(t *testing.T) {
	plainFake := &fakeOpenAI{t: t}
	plainSrv := httptest.NewServer(plainFake)
	defer plainSrv.Close()
	plain := openai.NewClient(option.WithBaseURL(plainSrv.URL+"/v1/"), option.WithAPIKey(testOpenAIKey), option.WithMaxRetries(0))
	_, _ = plain.Chat.Completions.New(context.Background(), toolParams("gpt-4.1-mini"))

	h := newHarness(t, nil)
	_, _ = h.client.Chat.Completions.New(context.Background(), toolParams("gpt-4.1-mini"))
	if string(plainFake.bodies[0]) != string(h.fake.bodies[0]) {
		t.Fatalf("body changed by the middleware:\n%s\n%s", plainFake.bodies[0], h.fake.bodies[0])
	}
}

// openai-go runs middleware once per HTTP attempt. A retried call is one span
// per attempt under the same parent; retries carry http.request.resend_count.
func TestRetriesAreSiblingAttemptsWithResendCount(t *testing.T) {
	h := newHarness(t, nil, option.WithMaxRetries(1))
	ctx, parent := h.tp.Tracer("app").Start(context.Background(), "agent")
	if _, err := h.client.Chat.Completions.New(ctx, chatParams("fixture-500-then-ok")); err != nil {
		t.Fatal(err)
	}
	parent.End()
	var attempts []sdktrace.ReadOnlySpan
	for _, s := range h.rec.Ended() {
		if strings.HasPrefix(s.Name(), "chat ") {
			attempts = append(attempts, s)
		}
	}
	if len(attempts) != 2 {
		t.Fatalf("attempt spans = %d, want 2", len(attempts))
	}
	if attempts[0].Status().Code != codes.Error || str(attrs(attempts[0]), traceaiopenai.ErrorType) != "server_error" {
		t.Error("first attempt must be ERROR")
	}
	if _, ok := attrs(attempts[0])[traceaiopenai.HTTPRequestResendCount]; ok {
		t.Error("first attempt has no resend count")
	}
	if str(attrs(attempts[1]), traceaiopenai.HTTPRequestResendCount) != "1" || attempts[1].Status().Code == codes.Error {
		t.Error("retry must carry http.request.resend_count=1 and succeed")
	}
	for _, s := range attempts {
		if s.Parent().SpanID() != parent.SpanContext().SpanID() {
			t.Error("attempts must share the caller's parent span")
		}
	}
}

func TestAzureHostIsLabelledAzure(t *testing.T) {
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	mw := traceaiopenai.Middleware(traceaiopenai.WithTracerProvider(tp))
	req, _ := http.NewRequest(http.MethodPost, "https://my-resource.openai.azure.com/openai/deployments/d/chat/completions?api-version=x",
		strings.NewReader(`{"model":"gpt-4.1","messages":[]}`))
	_, _ = mw(req, func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(`{}`))}, nil
	})
	if got := str(attrs(rec.Ended()[0]), traceaiopenai.GenAIProviderName); got != "azure" {
		t.Fatalf("provider = %q", got)
	}
}
