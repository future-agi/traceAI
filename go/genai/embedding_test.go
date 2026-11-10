package traceaigenai_test

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/genai"
)

func TestSDKEmbedContentTracesBatch(t *testing.T) {
	clearHideEnv(t)
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1beta/models/gemini-embedding-001:batchEmbedContents" {
			t.Errorf("unexpected SDK path %s", r.URL.Path)
		}
		if r.Header.Get("x-goog-api-key") != testGeminiKey {
			t.Error("SDK API key not preserved")
		}
		b, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(b), "first document") || !strings.Contains(string(b), "second document") {
			t.Errorf("SDK input not preserved: %s", b)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"embeddings":[{"values":[0.125,0.875]},{"values":[0.25,0.75]}]}`)
	}))
	defer srv.Close()
	client, err := genai.NewClient(context.Background(), &genai.ClientConfig{
		APIKey: testGeminiKey, Backend: genai.BackendGeminiAPI,
		HTTPClient:  traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)),
		HTTPOptions: genai.HTTPOptions{BaseURL: srv.URL + "/"},
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, parent := tp.Tracer("test").Start(context.Background(), "parent")
	ctx = traceaigenai.ContextWithSessionID(ctx, "embedding-session")
	resp, err := client.Models.EmbedContent(ctx, "gemini-embedding-001", []*genai.Content{
		genai.NewContentFromText("first document", genai.RoleUser),
		genai.NewContentFromText("second document", genai.RoleUser),
	}, &genai.EmbedContentConfig{TaskType: "RETRIEVAL_DOCUMENT", OutputDimensionality: genai.Ptr[int32](2), Title: "private-title"})
	if err != nil {
		t.Fatal(err)
	}
	if len(resp.Embeddings) != 2 || resp.Embeddings[0].Values[0] != 0.125 {
		t.Fatalf("response changed: %+v", resp)
	}
	spans := rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1 embedding span", len(spans))
	}
	s := spans[0]
	if s.Name() != "embeddings gemini-embedding-001" || s.SpanKind() != trace.SpanKindClient || s.Parent().SpanID() != parent.SpanContext().SpanID() {
		t.Fatalf("wrong span name, kind or parent: %s %s %v", s.Name(), s.SpanKind(), s.Parent())
	}
	a := attrs(s)
	for k, v := range map[string]string{
		"gen_ai.span.kind": "EMBEDDING", "gen_ai.operation.name": "embeddings", "gen_ai.provider.name": "google",
		"gen_ai.request.model": "gemini-embedding-001", "embedding.model_name": "gemini-embedding-001",
		"embedding.count": "2", "gen_ai.embeddings.dimension.count": "2", "session.id": "embedding-session",
	} {
		if a[k] != v {
			t.Errorf("%s=%q, want %q", k, a[k], v)
		}
	}
	if !strings.Contains(a["input.value"], "first document") || !strings.Contains(a["input.value"], "second document") {
		t.Errorf("missing embedding input: %s", a["input.value"])
	}
	for _, forbidden := range []string{testGeminiKey, "private-title", "0.125", "gen_ai.usage.output_tokens"} {
		if strings.Contains(everything(s), forbidden) {
			t.Errorf("unexpected captured value %q", forbidden)
		}
	}
	parent.End()
}
