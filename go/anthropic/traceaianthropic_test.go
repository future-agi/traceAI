package traceaianthropic_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/option"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	traceaianthropic "github.com/future-agi/traceAI/go/anthropic"
)

const (
	apiKey    = "sk-ant-test-CREDENTIAL-5e1f"
	inMarker  = "INMARKER-a7d3"
	outMarker = "OUTMARKER-c41e"
	errMarker = "ERRMARKER-9b2d"
	sdkScope  = "com.anthropic.sdk.go"
	model     = "claude-opus-5-5"
)

// mode is how the spans come about: the SDK opens its own span and the
// middleware enriches it, or the SDK's spans are off and the middleware opens one.
type mode struct {
	name  string
	scope string
	opts  func(tp *sdktrace.TracerProvider) []option.RequestOption
}

var modes = []mode{
	{"enrich-sdk-span", sdkScope, func(tp *sdktrace.TracerProvider) []option.RequestOption {
		return []option.RequestOption{option.WithTracerProvider(tp)}
	}},
	{"own-span", traceaianthropic.InstrumentationName, func(*sdktrace.TracerProvider) []option.RequestOption {
		return []option.RequestOption{option.WithoutOpenTelemetry()}
	}},
}

func fixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// server answers POST /v1/messages with body, as JSON or as an event stream.
type server struct {
	*httptest.Server
	mu     sync.Mutex
	bodies [][]byte
	hdrs   []http.Header
}

func newServer(t *testing.T, handler func(w http.ResponseWriter, r *http.Request, n int)) *server {
	t.Helper()
	s := &server{}
	var n atomic.Int32
	s.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		s.mu.Lock()
		s.bodies = append(s.bodies, b)
		s.hdrs = append(s.hdrs, r.Header.Clone())
		s.mu.Unlock()
		handler(w, r, int(n.Add(1)))
	}))
	t.Cleanup(s.Close)
	return s
}

func jsonHandler(status int, body []byte) func(http.ResponseWriter, *http.Request, int) {
	return func(w http.ResponseWriter, _ *http.Request, _ int) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		w.Write(body)
	}
}

func sseHandler(body []byte) func(http.ResponseWriter, *http.Request, int) {
	return func(w http.ResponseWriter, _ *http.Request, _ int) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		w.Write(body)
	}
}

type harness struct {
	rec    *tracetest.SpanRecorder
	tp     *sdktrace.TracerProvider
	client anthropic.Client
}

func newHarness(t *testing.T, m mode, url string, extra ...traceaianthropic.Option) *harness {
	t.Helper()
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	opts := append([]traceaianthropic.Option{traceaianthropic.WithTracerProvider(tp)}, extra...)
	clientOpts := append(m.opts(tp),
		option.WithBaseURL(url),
		option.WithAPIKey(apiKey),
		option.WithMaxRetries(0),
		traceaianthropic.ClientOption(opts...),
	)
	return &harness{rec: rec, tp: tp, client: anthropic.NewClient(clientOpts...)}
}

func params() anthropic.MessageNewParams {
	return anthropic.MessageNewParams{
		Model:       model,
		MaxTokens:   256,
		Temperature: anthropic.Float(0.2),
		System:      []anthropic.TextBlockParam{{Text: "You are terse. " + inMarker}},
		Messages: []anthropic.MessageParam{
			anthropic.NewUserMessage(anthropic.NewTextBlock("What is the weather in Paris? " + inMarker)),
		},
		Tools: []anthropic.ToolUnionParam{{OfTool: &anthropic.ToolParam{
			Name:        "get_weather",
			Description: anthropic.String("Weather for a city. " + inMarker),
			InputSchema: anthropic.ToolInputSchemaParam{Properties: map[string]any{"city": map[string]any{"type": "string"}}},
		}}},
	}
}

func attrs(s sdktrace.ReadOnlySpan) map[string]attribute.Value {
	m := map[string]attribute.Value{}
	for _, kv := range s.Attributes() {
		m[string(kv.Key)] = kv.Value
	}
	return m
}

// allText is every attribute value and event of the spans, for leak scans.
func allText(spans []sdktrace.ReadOnlySpan) string {
	var b strings.Builder
	for _, s := range spans {
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
	}
	return b.String()
}

func onlySpan(t *testing.T, h *harness, m mode) (sdktrace.ReadOnlySpan, map[string]attribute.Value) {
	t.Helper()
	spans := h.rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("want exactly one span, got %d: %v", len(spans), names(spans))
	}
	s := spans[0]
	if s.Name() != "anthropic.messages.create" {
		t.Errorf("span name %q", s.Name())
	}
	if got := s.InstrumentationScope().Name; got != m.scope {
		t.Errorf("scope %q, want %q", got, m.scope)
	}
	return s, attrs(s)
}

func names(spans []sdktrace.ReadOnlySpan) []string {
	var out []string
	for _, s := range spans {
		out = append(out, s.InstrumentationScope().Name+"/"+s.Name())
	}
	return out
}

func wantStr(t *testing.T, a map[string]attribute.Value, key, want string) {
	t.Helper()
	if got := a[key].AsString(); got != want {
		t.Errorf("%s = %q, want %q", key, got, want)
	}
}

func wantInt(t *testing.T, a map[string]attribute.Value, key string, want int64) {
	t.Helper()
	v, ok := a[key]
	if !ok {
		t.Errorf("%s missing", key)
		return
	}
	if got := v.AsInt64(); got != want {
		t.Errorf("%s = %d, want %d", key, got, want)
	}
}

func wantContains(t *testing.T, a map[string]attribute.Value, key, sub string) {
	t.Helper()
	if !strings.Contains(a[key].Emit(), sub) {
		t.Errorf("%s = %q, does not contain %q", key, a[key].Emit(), sub)
	}
}

func TestMessagesNewIsOneLLMSpan(t *testing.T) {
	for _, m := range modes {
		t.Run(m.name, func(t *testing.T) {
			srv := newServer(t, jsonHandler(200, fixture(t, "message_text.json")))
			h := newHarness(t, m, srv.URL, traceaianthropic.WithTraceConfig(traceaianthropic.TraceConfig{}))
			ctx := traceaianthropic.ContextWithUserID(traceaianthropic.ContextWithSessionID(context.Background(), "sess-42"), "user-7")
			msg, err := h.client.Messages.New(ctx, params())
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(msg.Content[0].Text, outMarker) {
				t.Fatalf("application got a changed response: %+v", msg.Content)
			}
			s, a := onlySpan(t, h, m)
			if s.SpanKind().String() != "client" {
				t.Errorf("span kind %v", s.SpanKind())
			}
			if s.Status().Code == codes.Error {
				t.Errorf("status %v", s.Status())
			}
			wantStr(t, a, "gen_ai.span.kind", "LLM")
			wantStr(t, a, "gen_ai.operation.name", "chat")
			wantStr(t, a, "gen_ai.provider.name", "anthropic")
			wantStr(t, a, "gen_ai.request.model", model)
			wantStr(t, a, "gen_ai.response.model", model)
			wantStr(t, a, "gen_ai.response.id", "msg_01FixtureText")
			// input_tokens counts cached tokens, as the GenAI conventions and the SDK do;
			// the cache counts stay on their own keys.
			wantInt(t, a, "gen_ai.usage.input_tokens", 12+100+20)
			wantInt(t, a, "gen_ai.usage.output_tokens", 9)
			wantInt(t, a, "gen_ai.usage.cache_read.input_tokens", 100)
			wantInt(t, a, "gen_ai.usage.cache_write.input_tokens", 20)
			if fr := a["gen_ai.response.finish_reasons"].AsStringSlice(); len(fr) != 1 || fr[0] != "end_turn" {
				t.Errorf("finish_reasons %v", fr)
			}
			wantContains(t, a, "input.value", inMarker)
			wantStr(t, a, "input.mime_type", "application/json")
			wantContains(t, a, "output.value", outMarker)
			wantContains(t, a, "gen_ai.input.messages", `"parts":[{"type":"text","content":"What is the weather in Paris? `+inMarker)
			wantContains(t, a, "gen_ai.system_instructions", inMarker)
			wantContains(t, a, "gen_ai.output.messages", outMarker)
			wantContains(t, a, "gen_ai.tool.definitions", "get_weather")
			wantContains(t, a, "gen_ai.request.parameters", `"max_tokens":256`)
			wantStr(t, a, "session.id", "sess-42")
			wantStr(t, a, "user.id", "user-7")
			if strings.Contains(allText(h.rec.Ended()), apiKey) {
				t.Error("the API key is on the span")
			}
			// The server got the request body that the SDK built, unchanged.
			if len(srv.bodies) != 1 || !bytes.Contains(srv.bodies[0], []byte(inMarker)) {
				t.Errorf("server bodies %q", srv.bodies)
			}
		})
	}
}

func TestBareContextHasNoSession(t *testing.T) {
	for _, m := range modes {
		t.Run(m.name, func(t *testing.T) {
			srv := newServer(t, jsonHandler(200, fixture(t, "message_text.json")))
			h := newHarness(t, m, srv.URL)
			if _, err := h.client.Messages.New(context.Background(), params()); err != nil {
				t.Fatal(err)
			}
			_, a := onlySpan(t, h, m)
			for _, k := range []string{"session.id", "user.id"} {
				if _, ok := a[k]; ok {
					t.Errorf("%s set on a bare context", k)
				}
			}
		})
	}
}

func TestStreamIsOneSpan(t *testing.T) {
	for _, m := range modes {
		t.Run(m.name, func(t *testing.T) {
			srv := newServer(t, sseHandler(fixture(t, "stream_text.sse")))
			h := newHarness(t, m, srv.URL)
			stream := h.client.Messages.NewStreaming(context.Background(), params())
			var text strings.Builder
			events := 0
			for stream.Next() {
				events++
				ev := stream.Current()
				if ev.Type == "content_block_delta" {
					text.WriteString(ev.Delta.Text)
				}
			}
			if err := stream.Err(); err != nil {
				t.Fatal(err)
			}
			stream.Close()
			if events < 5 || !strings.Contains(text.String(), outMarker) {
				t.Fatalf("application stream changed: %d events, %q", events, text.String())
			}
			s, a := onlySpan(t, h, m)
			if s.Status().Code == codes.Error {
				t.Errorf("status %v", s.Status())
			}
			wantInt(t, a, "gen_ai.usage.output_tokens", 9)
			wantInt(t, a, "gen_ai.usage.input_tokens", 132)
			wantContains(t, a, "output.value", `"text":"It is sunny in Paris. `+outMarker+`"`)
			wantContains(t, a, "gen_ai.output.messages", `"finish_reason":"end_turn"`)
			if m.name == "own-span" {
				if !a["gen_ai.request.stream"].AsBool() {
					t.Error("gen_ai.request.stream not set")
				}
			}
		})
	}
}

func TestToolUseIsRecordedWithoutToolSpan(t *testing.T) {
	cases := []struct {
		name    string
		handler func(t *testing.T) func(http.ResponseWriter, *http.Request, int)
		stream  bool
		callID  string
	}{
		{"new", func(t *testing.T) func(http.ResponseWriter, *http.Request, int) {
			return jsonHandler(200, fixture(t, "message_tool_use.json"))
		}, false, "toolu_01FixtureCall"},
		{"stream", func(t *testing.T) func(http.ResponseWriter, *http.Request, int) {
			return sseHandler(fixture(t, "stream_tool_use.sse"))
		}, true, "toolu_01FixtureStreamCall"},
	}
	for _, m := range modes {
		for _, c := range cases {
			t.Run(m.name+"/"+c.name, func(t *testing.T) {
				srv := newServer(t, c.handler(t))
				h := newHarness(t, m, srv.URL)
				if c.stream {
					st := h.client.Messages.NewStreaming(context.Background(), params())
					acc := anthropic.Message{}
					for st.Next() {
						if err := acc.Accumulate(st.Current()); err != nil {
							t.Fatal(err)
						}
					}
					st.Close()
					if len(acc.Content) != 1 || acc.Content[0].Name != "get_weather" {
						t.Fatalf("application got %+v", acc.Content)
					}
				} else if _, err := h.client.Messages.New(context.Background(), params()); err != nil {
					t.Fatal(err)
				}
				_, a := onlySpan(t, h, m)
				wantContains(t, a, "gen_ai.output.messages", `{"type":"tool_call","id":"`+c.callID+`","name":"get_weather","arguments":{"city":"Paris"}}`)
				if fr := a["gen_ai.response.finish_reasons"].AsStringSlice(); len(fr) != 1 || fr[0] != "tool_use" {
					t.Errorf("finish_reasons %v", fr)
				}
			})
		}
	}
}

func TestAPIErrorIsErrorWithoutMessageText(t *testing.T) {
	cases := []struct {
		status  int
		file    string
		errType string
	}{
		{401, "error_401.json", "authentication_error"},
		{429, "error_429.json", "rate_limit_error"},
	}
	for _, m := range modes {
		for _, c := range cases {
			t.Run(m.name+"/"+c.errType, func(t *testing.T) {
				srv := newServer(t, jsonHandler(c.status, fixture(t, c.file)))
				h := newHarness(t, m, srv.URL, traceaianthropic.WithTraceConfig(traceaianthropic.TraceConfig{HideOutputs: true}))
				_, err := h.client.Messages.New(context.Background(), params())
				var apiErr *anthropic.Error
				if !errorsAs(err, &apiErr) || apiErr.StatusCode != c.status {
					t.Fatalf("application error %v", err)
				}
				s, a := onlySpan(t, h, m)
				if s.Status().Code != codes.Error {
					t.Errorf("status %v", s.Status())
				}
				wantStr(t, a, "error.type", c.errType)
				wantInt(t, a, "http.response.status_code", int64(c.status))
				all := allText(h.rec.Ended())
				for _, leak := range []string{errMarker, apiKey} {
					if strings.Contains(all, leak) {
						t.Errorf("span carries %q:\n%s", leak, all)
					}
				}
			})
		}
	}
}

func TestAPIErrorBodyIsOutputByDefault(t *testing.T) {
	srv := newServer(t, jsonHandler(429, fixture(t, "error_429.json")))
	h := newHarness(t, modes[1], srv.URL, traceaianthropic.WithTraceConfig(traceaianthropic.TraceConfig{}))
	_, _ = h.client.Messages.New(context.Background(), params())
	_, a := onlySpan(t, h, modes[1])
	wantContains(t, a, "output.value", errMarker)
	if s := h.rec.Ended()[0].Status().Description; strings.Contains(s, errMarker) {
		t.Errorf("status description carries the message: %q", s)
	}
}

func errorsAs(err error, target **anthropic.Error) bool {
	for e := err; e != nil; {
		if v, ok := e.(*anthropic.Error); ok {
			*target = v
			return true
		}
		u, ok := e.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		e = u.Unwrap()
	}
	return false
}

func TestStreamCancellationEndsSpan(t *testing.T) {
	head := bytes.SplitAfter(fixture(t, "stream_text.sse"), []byte("\n\n"))
	for _, m := range modes {
		t.Run(m.name, func(t *testing.T) {
			release := make(chan struct{})
			srv := newServer(t, func(w http.ResponseWriter, r *http.Request, _ int) {
				w.Header().Set("Content-Type", "text/event-stream")
				w.WriteHeader(200)
				w.Write(bytes.Join(head[:3], nil))
				w.(http.Flusher).Flush()
				select {
				case <-r.Context().Done():
				case <-release:
				}
			})
			defer close(release)
			h := newHarness(t, m, srv.URL)
			ctx, cancel := context.WithCancel(context.Background())
			stream := h.client.Messages.NewStreaming(ctx, params())
			if !stream.Next() {
				t.Fatalf("no first event: %v", stream.Err())
			}
			cancel()
			done := make(chan struct{})
			go func() {
				for stream.Next() {
				}
				stream.Close()
				close(done)
			}()
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				t.Fatal("stream did not return after cancel")
			}
			s, a := onlySpan(t, h, m)
			if m.name == "own-span" {
				if s.Status().Code != codes.Error {
					t.Errorf("status %v", s.Status())
				}
				wantStr(t, a, "error.type", "canceled")
			}
		})
	}
}

func TestStreamCanceledWhileNotReadingEndsOwnSpan(t *testing.T) {
	head := bytes.SplitAfter(fixture(t, "stream_text.sse"), []byte("\n\n"))
	release := make(chan struct{})
	srv := newServer(t, func(w http.ResponseWriter, r *http.Request, _ int) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		w.Write(bytes.Join(head[:2], nil))
		w.(http.Flusher).Flush()
		select {
		case <-r.Context().Done():
		case <-release:
		}
	})
	defer close(release)
	h := newHarness(t, modes[1], srv.URL)
	ctx, cancel := context.WithCancel(context.Background())
	stream := h.client.Messages.NewStreaming(ctx, params())
	if !stream.Next() {
		t.Fatal(stream.Err())
	}
	cancel() // and never read or close again
	deadline := time.Now().Add(5 * time.Second)
	for len(h.rec.Ended()) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	s, _ := onlySpan(t, h, modes[1])
	if s.Status().Code != codes.Error {
		t.Errorf("status %v", s.Status())
	}
	stream.Close()
}

func TestStreamClosedEarlyIsCanceled(t *testing.T) {
	head := bytes.SplitAfter(fixture(t, "stream_text.sse"), []byte("\n\n"))
	release := make(chan struct{})
	defer close(release)
	srv := newServer(t, func(w http.ResponseWriter, r *http.Request, _ int) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		w.Write(bytes.Join(head[:3], nil))
		w.(http.Flusher).Flush()
		select {
		case <-r.Context().Done():
		case <-release:
		}
	})
	h := newHarness(t, modes[1], srv.URL)
	stream := h.client.Messages.NewStreaming(context.Background(), params())
	if !stream.Next() {
		t.Fatal(stream.Err())
	}
	stream.Close()
	s, a := onlySpan(t, h, modes[1])
	if s.Status().Code != codes.Error {
		t.Errorf("status %v", s.Status())
	}
	wantStr(t, a, "error.type", "canceled")
}

func TestStreamErrorEventAndCutShort(t *testing.T) {
	cases := map[string]string{"stream_error_event.sse": "overloaded_error", "stream_cut_short.sse": "api_error"}
	for file, errType := range cases {
		t.Run(file, func(t *testing.T) {
			srv := newServer(t, sseHandler(fixture(t, file)))
			h := newHarness(t, modes[1], srv.URL)
			stream := h.client.Messages.NewStreaming(context.Background(), params())
			for stream.Next() {
			}
			stream.Close()
			s, a := onlySpan(t, h, modes[1])
			if s.Status().Code != codes.Error {
				t.Errorf("status %v", s.Status())
			}
			wantStr(t, a, "error.type", errType)
			if strings.Contains(allText(h.rec.Ended()), errMarker) {
				t.Error("error message text on the span")
			}
		})
	}
}

func TestHideFlagsFromEnvironment(t *testing.T) {
	cases := []struct {
		name    string
		env     map[string]string
		absent  []string
		present []string
		check   func(t *testing.T, a map[string]attribute.Value)
	}{
		{
			name:   "hide inputs and outputs",
			env:    map[string]string{"FI_HIDE_INPUTS": "true", "FI_HIDE_OUTPUTS": "TRUE"},
			absent: []string{inMarker, outMarker, "input.mime_type", "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions", "gen_ai.tool.definitions"},
			check: func(t *testing.T, a map[string]attribute.Value) {
				wantStr(t, a, "input.value", "__REDACTED__")
				wantStr(t, a, "output.value", "__REDACTED__")
				wantInt(t, a, "gen_ai.usage.output_tokens", 9)
				wantStr(t, a, "gen_ai.request.model", model)
			},
		},
		{
			name:    "hide messages only",
			env:     map[string]string{"FI_HIDE_INPUT_MESSAGES": "true", "FI_HIDE_OUTPUT_MESSAGES": "true"},
			absent:  []string{"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions"},
			present: []string{"input.value", "output.value"},
		},
		{
			name:   "hide text",
			env:    map[string]string{"FI_HIDE_INPUT_TEXT": "true", "FI_HIDE_OUTPUT_TEXT": "true"},
			absent: []string{"content\":\"What is the weather", "Paris. " + outMarker + "\"}],\"finish"},
			check: func(t *testing.T, a map[string]attribute.Value) {
				wantContains(t, a, "gen_ai.input.messages", `"content":"__REDACTED__"`)
				wantContains(t, a, "gen_ai.output.messages", `"content":"__REDACTED__"`)
			},
		},
		{
			name:   "hide invocation parameters",
			env:    map[string]string{"FI_HIDE_LLM_INVOCATION_PARAMETERS": "true"},
			absent: []string{"gen_ai.request.parameters"},
		},
		{
			name:    "unparsable value keeps the default",
			env:     map[string]string{"FI_HIDE_INPUTS": "yes"},
			present: []string{inMarker},
		},
	}
	for _, m := range modes {
		for _, c := range cases {
			t.Run(m.name+"/"+c.name, func(t *testing.T) {
				for k, v := range c.env {
					t.Setenv(k, v)
				}
				srv := newServer(t, jsonHandler(200, fixture(t, "message_text.json")))
				h := newHarness(t, m, srv.URL) // reads FI_HIDE_* now
				if _, err := h.client.Messages.New(context.Background(), params()); err != nil {
					t.Fatal(err)
				}
				_, a := onlySpan(t, h, m)
				all := allText(h.rec.Ended())
				for _, s := range c.absent {
					if strings.Contains(all, s) {
						t.Errorf("%q exported:\n%s", s, all)
					}
				}
				for _, s := range c.present {
					if !strings.Contains(all, s) {
						t.Errorf("%q missing", s)
					}
				}
				if c.check != nil {
					c.check(t, a)
				}
			})
		}
	}
}

func TestTraceConfigFromEnv(t *testing.T) {
	t.Setenv("FI_HIDE_INPUTS", "True")
	t.Setenv("FI_HIDE_OUTPUTS", "false")
	t.Setenv("FI_HIDE_INPUT_IMAGES", "1")
	t.Setenv("FI_BASE64_IMAGE_MAX_LENGTH", "64")
	c := traceaianthropic.TraceConfigFromEnv()
	if !c.HideInputs || c.HideOutputs || c.HideInputImages || c.Base64ImageMaxLength != 64 {
		t.Errorf("%+v", c)
	}
	if (traceaianthropic.TraceConfig{}) != (traceaianthropic.TraceConfig{HideInputs: false}) {
		t.Error("zero value must capture everything")
	}
}

func TestBase64ImagesAreBoundedAndHideable(t *testing.T) {
	big := strings.Repeat("QUJD", 40) // 160 base64 bytes
	p := params()
	p.Messages = []anthropic.MessageParam{anthropic.NewUserMessage(
		anthropic.NewImageBlockBase64("image/png", big),
		anthropic.NewTextBlock("Describe it. "+inMarker),
	)}
	for _, c := range []struct {
		name string
		cfg  traceaianthropic.TraceConfig
		part bool
	}{
		{"over max length", traceaianthropic.TraceConfig{Base64ImageMaxLength: 64}, true},
		{"hide input images", traceaianthropic.TraceConfig{HideInputImages: true}, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			srv := newServer(t, jsonHandler(200, fixture(t, "message_text.json")))
			h := newHarness(t, modes[1], srv.URL, traceaianthropic.WithTraceConfig(c.cfg))
			if _, err := h.client.Messages.New(context.Background(), p); err != nil {
				t.Fatal(err)
			}
			_, a := onlySpan(t, h, modes[1])
			if strings.Contains(allText(h.rec.Ended()), big) {
				t.Error("image bytes exported")
			}
			wantContains(t, a, "input.value", "__REDACTED__")
			wantContains(t, a, "input.value", inMarker)
			if got := strings.Contains(a["gen_ai.input.messages"].AsString(), "anthropic_image"); got != c.part {
				t.Errorf("image part present=%v, want %v", got, c.part)
			}
			if !bytes.Contains(srv.bodies[0], []byte(big)) {
				t.Error("the request sent to the API lost the image")
			}
		})
	}
}

func TestOtherEndpointsAreNotSpanned(t *testing.T) {
	srv := newServer(t, jsonHandler(200, []byte(`{"input_tokens":7}`)))
	h := newHarness(t, modes[1], srv.URL)
	if _, err := h.client.Messages.CountTokens(context.Background(), anthropic.MessageCountTokensParams{
		Model:    model,
		Messages: []anthropic.MessageParam{anthropic.NewUserMessage(anthropic.NewTextBlock("hi"))},
	}); err != nil {
		t.Fatal(err)
	}
	if n := len(h.rec.Ended()); n != 0 {
		t.Errorf("count_tokens produced %d spans: %v", n, names(h.rec.Ended()))
	}
}

func TestRetries(t *testing.T) {
	handler := func(w http.ResponseWriter, _ *http.Request, n int) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("retry-after-ms", "1")
		if n == 1 {
			w.WriteHeader(529)
			w.Write(fixture(t, "error_529.json"))
			return
		}
		w.Write(fixture(t, "message_text.json"))
	}
	t.Run("own span per attempt", func(t *testing.T) {
		srv := newServer(t, handler)
		h := newHarness(t, modes[1], srv.URL)
		if _, err := h.client.Messages.New(context.Background(), params(), option.WithMaxRetries(1)); err != nil {
			t.Fatal(err)
		}
		spans := h.rec.Ended()
		if len(spans) != 2 {
			t.Fatalf("want one span per attempt, got %d", len(spans))
		}
		first, second := attrs(spans[0]), attrs(spans[1])
		wantStr(t, first, "error.type", "overloaded_error")
		wantInt(t, second, "http.request.resend_count", 1)
		if spans[1].Status().Code == codes.Error {
			t.Error("retried attempt failed")
		}
	})
	t.Run("one sdk span for the call", func(t *testing.T) {
		srv := newServer(t, handler)
		h := newHarness(t, modes[0], srv.URL)
		if _, err := h.client.Messages.New(context.Background(), params(), option.WithMaxRetries(1)); err != nil {
			t.Fatal(err)
		}
		_, a := onlySpan(t, h, modes[0])
		wantContains(t, a, "output.value", outMarker)
	})
}

// A body that the middleware does not decode, such as Amazon Bedrock's binary
// event stream, must reach the application without being read first.
func TestUndecodedBodyIsNotBuffered(t *testing.T) {
	mw := traceaianthropic.Middleware(traceaianthropic.WithTraceConfig(traceaianthropic.TraceConfig{}))
	req := httptestRequest(t)
	body := &countingBody{r: strings.NewReader("binary-event-stream")}
	res, err := mw(req, func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/vnd.amazon.eventstream"}}, Body: body}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if body.reads != 0 {
		t.Errorf("middleware read the body %d times before the application", body.reads)
	}
	if b, _ := io.ReadAll(res.Body); string(b) != "binary-event-stream" {
		t.Errorf("body %q", b)
	}
}

type countingBody struct {
	r     io.Reader
	reads int
}

func (c *countingBody) Read(p []byte) (int, error) { c.reads++; return c.r.Read(p) }
func (c *countingBody) Close() error               { return nil }

func httptestRequest(t *testing.T) *http.Request {
	t.Helper()
	b, _ := json.Marshal(map[string]any{"model": model, "max_tokens": 8, "messages": []any{}})
	req, err := http.NewRequest(http.MethodPost, "https://api.anthropic.com/v1/messages", bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	return req
}

// Credentials in a request body, such as the authorization_token of an MCP
// server, never reach a span, even with content capture on.
func TestRequestCredentialsAreNeverExported(t *testing.T) {
	const token = "mcp-token-SECRET-77f1"
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	mw := traceaianthropic.Middleware(traceaianthropic.WithTracerProvider(tp), traceaianthropic.WithTraceConfig(traceaianthropic.TraceConfig{}))
	b, _ := json.Marshal(map[string]any{
		"model": model, "max_tokens": 8,
		"messages":    []any{map[string]any{"role": "user", "content": "hi " + inMarker}},
		"mcp_servers": []any{map[string]any{"type": "url", "url": "https://mcp.example.com/sse", "name": "ex", "authorization_token": token}},
	})
	req, _ := http.NewRequest(http.MethodPost, "https://api.anthropic.com/v1/messages?beta=true", bytes.NewReader(b))
	var sent []byte
	_, err := mw(req, func(r *http.Request) (*http.Response, error) {
		sent, _ = io.ReadAll(r.Body)
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(bytes.NewReader(fixture(t, "message_text.json")))}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(sent, []byte(token)) {
		t.Error("the request sent to the API lost the token")
	}
	spans := rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("%d spans", len(spans))
	}
	all := allText(spans)
	if strings.Contains(all, token) {
		t.Errorf("token exported:\n%s", all)
	}
	if !strings.Contains(all, inMarker) || !strings.Contains(all, `"authorization_token":"__REDACTED__"`) {
		t.Errorf("input.value lost its content or the redaction:\n%s", all)
	}
}

func TestSDKContentModeWithHideFlagsWarns(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
	t.Setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
	traceaianthropic.Middleware(traceaianthropic.WithTraceConfig(traceaianthropic.TraceConfig{}))
	if buf.Len() != 0 {
		t.Errorf("warned without a hide flag: %s", buf.String())
	}
	traceaianthropic.Middleware(traceaianthropic.WithTraceConfig(traceaianthropic.TraceConfig{HideInputs: true}))
	if !strings.Contains(buf.String(), "ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE=content") {
		t.Errorf("no warning: %q", buf.String())
	}
}
