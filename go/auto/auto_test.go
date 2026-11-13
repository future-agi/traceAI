package auto_test

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	anthropic "github.com/anthropics/anthropic-sdk-go"
	anthropicoption "github.com/anthropics/anthropic-sdk-go/option"
	"github.com/future-agi/traceAI/go/auto"
	"github.com/openai/openai-go/v3"
	openaioption "github.com/openai/openai-go/v3/option"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

const requestJSON = `{"model":"test-model","messages":[{"role":"user","content":"Hi"}]}`
const responseJSON = `{"id":"c1","model":"test-model","choices":[{"index":0,"message":{"role":"assistant","content":"Hello"},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}`

func setup(t *testing.T, opts ...auto.Option) (*tracetest.SpanRecorder, *sdktrace.TracerProvider) {
	t.Helper()
	_ = auto.Shutdown(context.Background())
	old := otel.GetTracerProvider()
	base := http.DefaultTransport
	p := sdktrace.NewTracerProvider()
	otel.SetTracerProvider(p)
	rec := tracetest.NewSpanRecorder()
	shutdown, err := auto.InitForTest(context.Background(), append([]auto.Option{auto.WithSpanProcessor(rec)}, opts...)...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = shutdown(context.Background())
		_ = p.Shutdown(context.Background())
		http.DefaultTransport = base
		otel.SetTracerProvider(old)
	})
	return rec, p
}
func value(s sdktrace.ReadOnlySpan, k string) any {
	for _, v := range s.Attributes() {
		if string(v.Key) == k {
			return v.Value.AsInterface()
		}
	}
	return nil
}
func wires(rec *tracetest.SpanRecorder) []sdktrace.ReadOnlySpan {
	out := []sdktrace.ReadOnlySpan{}
	for _, s := range rec.Ended() {
		if value(s, "traceai.capture.layer") == "wire" {
			out = append(out, s)
		}
	}
	return out
}
func localServer(t *testing.T, body, ct string, status int) *httptest.Server {
	t.Helper()
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		if string(b) != requestJSON {
			t.Errorf("application request changed: %s", b)
		}
		w.Header().Set("Content-Type", ct)
		w.WriteHeader(status)
		if strings.Contains(ct, "event-stream") {
			for _, line := range strings.SplitAfter(body, "\n\n") {
				_, _ = io.WriteString(w, line)
				w.(http.Flusher).Flush()
			}
		} else {
			_, _ = io.WriteString(w, body)
		}
	}))
	t.Cleanup(s.Close)
	return s
}
func TestInitPlainHTTPAndParent(t *testing.T) {
	rec, p := setup(t)
	server := localServer(t, responseJSON, "application/json", 200)
	ctx, parent := p.Tracer("test").Start(context.Background(), "framework", trace.WithAttributes(attribute.String("gen_ai.span.kind", "AGENT")))
	ctx = auto.ContextWithSessionID(ctx, "session-1")
	before := time.Now()
	req, _ := http.NewRequestWithContext(ctx, "POST", server.URL+"/v1/chat/completions", strings.NewReader(requestJSON))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	data, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil || string(data) != responseJSON {
		t.Fatalf("response changed %q %v", data, err)
	}
	parent.End()
	spans := wires(rec)
	if len(spans) != 1 {
		t.Fatalf("want 1 wire span got %d", len(spans))
	}
	s := spans[0]
	if s.Name() != "chat test-model" || s.Parent().SpanID() != parent.SpanContext().SpanID() || s.SpanKind() != trace.SpanKindClient || s.Status().Code != codes.Ok {
		t.Fatalf("incorrect span %s", s.Name())
	}
	if s.StartTime().Before(before) || s.StartTime().After(s.EndTime()) {
		t.Fatal("invalid explicit times")
	}
	for k, want := range map[string]any{"output.value": "Hello", "gen_ai.usage.total_tokens": int64(3), "session.id": "<redacted>"} {
		if got := value(s, k); got != want {
			t.Errorf("%s = %v want %v", k, got, want)
		}
	}
	if len(auto.Report().Integrations) != 6 {
		t.Fatal("incomplete report")
	}
}
func TestStreamingUnchanged(t *testing.T) {
	rec, _ := setup(t)
	body := "data: {\"id\":\"c1\",\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"Hello\"}}]}\n\ndata: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
	server := localServer(t, body, "text/event-stream", 200)
	resp, err := http.Post(server.URL+"/v1/chat/completions", "application/json", strings.NewReader(requestJSON))
	if err != nil {
		t.Fatal(err)
	}
	var got bytes.Buffer
	buffer := make([]byte, 3)
	if _, err = io.CopyBuffer(&got, resp.Body, buffer); err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if got.String() != body {
		t.Fatal("stream bytes changed")
	}
	ss := wires(rec)
	if len(ss) != 1 || value(ss[0], "output.value") != "Hello" || value(ss[0], "gen_ai.request.stream") != true || value(ss[0], "gen_ai.response.time_to_first_chunk") == nil {
		t.Fatal("stream capture missing")
	}
}
func TestDedupAndSuppression(t *testing.T) {
	for _, kind := range []string{"LLM", "EMBEDDING", "RERANKER", "GUARDRAIL", "suppressed"} {
		t.Run(kind, func(t *testing.T) {
			rec, p := setup(t)
			server := localServer(t, responseJSON, "application/json", 200)
			ctx := context.Background()
			if kind == "suppressed" {
				ctx = auto.SuppressInstrumentation(ctx)
			} else {
				var span trace.Span
				ctx, span = p.Tracer("library").Start(ctx, "library", trace.WithAttributes(attribute.String("gen_ai.span.kind", kind)))
				defer span.End()
			}
			req, _ := http.NewRequestWithContext(ctx, "POST", server.URL+"/v1/chat/completions", strings.NewReader(requestJSON))
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if len(wires(rec)) != 0 {
				t.Fatal("duplicate wire span")
			}
		})
	}
}
func TestRealSDKDefaults(t *testing.T) {
	for _, sdk := range []string{"openai", "anthropic"} {
		t.Run(sdk, func(t *testing.T) {
			rec, _ := setup(t)
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.Copy(io.Discard, r.Body)
				w.Header().Set("Content-Type", "application/json")
				if sdk == "openai" {
					_, _ = io.WriteString(w, responseJSON)
				} else {
					_, _ = io.WriteString(w, `{"id":"msg_1","type":"message","role":"assistant","model":"test-model","content":[{"type":"text","text":"Hello"}],"stop_reason":"end_turn","usage":{"input_tokens":2,"output_tokens":1}}`)
				}
			}))
			defer s.Close()
			if sdk == "openai" {
				c := openai.NewClient(openaioption.WithAPIKey("sdk-test"), openaioption.WithBaseURL(s.URL+"/v1/"), openaioption.WithMaxRetries(0))
				_, err := c.Chat.Completions.New(context.Background(), openai.ChatCompletionNewParams{Model: "test-model", Messages: []openai.ChatCompletionMessageParamUnion{openai.UserMessage("Hi")}})
				if err != nil {
					t.Fatal(err)
				}
			} else {
				c := anthropic.NewClient(anthropicoption.WithAPIKey("sdk-test"), anthropicoption.WithBaseURL(s.URL), anthropicoption.WithMaxRetries(0))
				_, err := c.Messages.New(context.Background(), anthropic.MessageNewParams{Model: "test-model", MaxTokens: 16, Messages: []anthropic.MessageParam{anthropic.NewUserMessage(anthropic.NewTextBlock("Hi"))}})
				if err != nil {
					t.Fatal(err)
				}
			}
			ss := wires(rec)
			if len(ss) != 1 || value(ss[0], "output.value") != "Hello" {
				t.Fatalf("SDK default transport not covered: %d", len(ss))
			}
		})
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func TestCustomTransportNestedAndErrors(t *testing.T) {
	rec, _ := setup(t)
	base := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		_, _ = io.Copy(io.Discard, r.Body)
		return &http.Response{StatusCode: 401, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"error":{"message":"denied"}}`))}, nil
	}))
	// A wrapping transport passes the marked context into another capture.
	outer := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) { return base.RoundTrip(r) }))
	req, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(requestJSON))
	req.Header.Set("Content-Type", "application/json")
	resp, err := outer.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	ss := wires(rec)
	if len(ss) != 1 || ss[0].Status().Description != "denied" {
		t.Fatalf("nested or error capture failed %d", len(ss))
	}
	broken := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) { return nil, errors.New("transport contains secret") }))
	_, _ = broken.RoundTrip(req)
	ss = wires(rec)
	if len(ss) != 2 || ss[1].Status().Description != "transport error" {
		t.Fatal("transport error handling")
	}
}
