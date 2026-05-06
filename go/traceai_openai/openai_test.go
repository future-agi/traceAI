package traceai_openai

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func setupTracer() (*tracetest.InMemoryExporter, *sdktrace.TracerProvider) {
	exporter := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithSyncer(exporter),
	)
	return exporter, tp
}

func TestMiddleware_ChatCompletion(t *testing.T) {
	exporter, tp := setupTracer()
	defer tp.Shutdown(context.Background())

	mw := Middleware(WithTracerProvider(tp))

	reqBody := `{"model":"gpt-4","messages":[{"role":"user","content":"hello"}],"temperature":0.7}`
	respBody := `{"id":"chatcmpl-abc","model":"gpt-4","choices":[{"finish_reason":"stop","message":{"content":"Hi there!"}}],"usage":{"prompt_tokens":5,"completion_tokens":3}}`

	req, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", io.NopCloser(strings.NewReader(reqBody)))

	mockNext := func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: 200,
			Body:       io.NopCloser(strings.NewReader(respBody)),
		}, nil
	}

	resp, err := mw(req, mockNext)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp.StatusCode != 200 {
		t.Fatalf("expected status 200, got %d", resp.StatusCode)
	}

	spans := exporter.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("expected 1 span, got %d", len(spans))
	}

	span := spans[0]
	if span.Name != "openai.chat" {
		t.Errorf("expected span name 'openai.chat', got '%s'", span.Name)
	}
	if span.Status.Code != codes.Ok {
		t.Errorf("expected OK status, got %v", span.Status.Code)
	}

	assertAttr(t, span.Attributes, "gen_ai.system", "openai")
	assertAttr(t, span.Attributes, "gen_ai.request.model", "gpt-4")
	assertAttr(t, span.Attributes, "gen_ai.response.model", "gpt-4")
	assertAttr(t, span.Attributes, "gen_ai.response.id", "chatcmpl-abc")
	assertIntAttr(t, span.Attributes, "gen_ai.usage.input_tokens", 5)
	assertIntAttr(t, span.Attributes, "gen_ai.usage.output_tokens", 3)
}

func TestMiddleware_NonAIEndpoint(t *testing.T) {
	exporter, tp := setupTracer()
	defer tp.Shutdown(context.Background())

	mw := Middleware(WithTracerProvider(tp))
	req, _ := http.NewRequest("GET", "https://api.openai.com/v1/models", nil)

	_, err := mw(req, func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: http.NoBody}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if n := len(exporter.GetSpans()); n != 0 {
		t.Fatalf("got %d spans, want 0", n)
	}
}

func TestMiddleware_ErrorResponse(t *testing.T) {
	exporter, tp := setupTracer()
	defer tp.Shutdown(context.Background())

	mw := Middleware(WithTracerProvider(tp))

	reqBody := `{"model":"gpt-4","messages":[{"role":"user","content":"test"}]}`
	req, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", io.NopCloser(strings.NewReader(reqBody)))

	mockNext := func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: 429,
			Status:     "429 Too Many Requests",
			Body:       io.NopCloser(strings.NewReader(`{"error":{"message":"rate limited"}}`)),
		}, nil
	}

	resp, err := mw(req, mockNext)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp.StatusCode != 429 {
		t.Fatalf("expected 429, got %d", resp.StatusCode)
	}

	spans := exporter.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("expected 1 span, got %d", len(spans))
	}

	if spans[0].Status.Code != codes.Error {
		t.Errorf("expected Error status for 429, got %v", spans[0].Status.Code)
	}
}

func TestMiddleware_ContentCaptureDisabled(t *testing.T) {
	exporter, tp := setupTracer()
	defer tp.Shutdown(context.Background())

	mw := Middleware(WithTracerProvider(tp), WithContentCapture(false))

	reqBody := `{"model":"gpt-4","messages":[{"role":"user","content":"secret stuff"}]}`
	respBody := `{"id":"chatcmpl-abc","model":"gpt-4","choices":[{"finish_reason":"stop","message":{"content":"response"}}],"usage":{"prompt_tokens":5,"completion_tokens":3}}`

	req, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", io.NopCloser(strings.NewReader(reqBody)))
	mockNext := func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: 200,
			Body:       io.NopCloser(strings.NewReader(respBody)),
		}, nil
	}

	_, err := mw(req, mockNext)
	if err != nil {
		t.Fatal(err)
	}

	spans := exporter.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("expected 1 span, got %d", len(spans))
	}

	for _, a := range spans[0].Attributes {
		if string(a.Key) == "gen_ai.prompt" || string(a.Key) == "gen_ai.completion" {
			t.Errorf("content capture disabled but found attribute %s", a.Key)
		}
	}
}

func assertAttr(t *testing.T, attrs []attribute.KeyValue, key, want string) {
	t.Helper()
	for _, a := range attrs {
		if string(a.Key) == key {
			got := a.Value.AsString()
			if got != want {
				t.Errorf("attr %s: got %q, want %q", key, got, want)
			}
			return
		}
	}
	t.Errorf("attr %s not found", key)
}

func assertIntAttr(t *testing.T, attrs []attribute.KeyValue, key string, want int) {
	t.Helper()
	for _, a := range attrs {
		if string(a.Key) == key {
			got := a.Value.AsInt64()
			if got != int64(want) {
				t.Errorf("attr %s: got %d, want %d", key, got, want)
			}
			return
		}
	}
	t.Errorf("attr %s not found", key)
}
