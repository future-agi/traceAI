package main

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

type modelRoundTripper func(*http.Request) (*http.Response, error)

func (f modelRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// The demo owns its LLM span; the A2A interceptor must not invent an LLM
// provider. Only the actual OpenAI host can be labelled OpenAI automatically.
func TestDemoModelProviderMatchesEndpoint(t *testing.T) {
	for _, tc := range []struct {
		name, base, provider string
		status               int
	}{
		{"default", "", "openai", http.StatusOK},
		{"explicit-openai", "https://api.openai.com/v1/", "openai", http.StatusOK},
		{"case-insensitive-host", "https://API.OPENAI.COM:443/v1", "openai", http.StatusOK},
		{"openai-error", "https://api.openai.com/v1", "openai", http.StatusNotFound},
		{"compatible-provider", "https://compatible.example/v1", "", http.StatusOK},
		{"lookalike-host", "https://api.openai.com.example/v1", "", http.StatusOK},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("OPENAI_API_KEY", "test-model-key")
			t.Setenv("OPENAI_BASE_URL", tc.base)
			recorder := tracetest.NewSpanRecorder()
			tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
			t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
			previous := http.DefaultClient
			http.DefaultClient = &http.Client{Transport: modelRoundTripper(func(r *http.Request) (*http.Response, error) {
				if r.Method != http.MethodPost || r.URL.Path != "/v1/chat/completions" {
					t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
				}
				return &http.Response{StatusCode: tc.status, Body: io.NopCloser(strings.NewReader(`{"model":"gpt-4.1-mini","choices":[{"message":{"content":"A short trip plan"}}],"usage":{"prompt_tokens":8,"completion_tokens":5,"total_tokens":13}}`)), Header: make(http.Header)}, nil
			})}
			t.Cleanup(func() { http.DefaultClient = previous })
			agent := &demoAgent{tracer: tp.Tracer("demo-provider-test"), tc: traceaia2a.TraceConfig{}}
			_, err := agent.answer(context.Background(), "Plan a short trip")
			if (err != nil) != (tc.status != http.StatusOK) {
				t.Fatalf("model error = %v; status %d", err, tc.status)
			}
			spans := recorder.Ended()
			if len(spans) != 1 {
				t.Fatalf("want one model span, got %d", len(spans))
			}
			attrs := map[attribute.Key]attribute.Value{}
			for _, a := range spans[0].Attributes() {
				attrs[a.Key] = a.Value
			}
			value, present := attrs["gen_ai.provider.name"]
			if tc.provider == "" {
				if present {
					t.Fatalf("compatible endpoint falsely labelled %q", value.AsString())
				}
			} else if !present || value.AsString() != tc.provider {
				t.Fatalf("gen_ai.provider.name = %q (present %t), want %q", value.AsString(), present, tc.provider)
			}
			if attrs["gen_ai.span.kind"].AsString() != "LLM" {
				t.Fatal("demo model span lost LLM type")
			}
			for _, a := range spans[0].Attributes() {
				if strings.Contains(a.Value.Emit(), "test-model-key") {
					t.Fatal("model key exported")
				}
			}
		})
	}
}
