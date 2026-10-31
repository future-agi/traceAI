package traceaiopenai_test

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	traceaiopenai "github.com/future-agi/traceAI/go/openai"
	"github.com/openai/openai-go/v3"
	"github.com/openai/openai-go/v3/option"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	collectortrace "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

func TestExtendedOTLPWirePrivacyAndShape(t *testing.T) {
	for _, hidden := range []bool{false, true} {
		t.Run(fmt.Sprint(hidden), func(t *testing.T) {
			requests := make(chan *collectortrace.ExportTraceServiceRequest, 8)
			collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/tracer/v1/traces" || r.Header.Get("Content-Type") != "application/x-protobuf" || r.Header.Get("Content-Encoding") != "" {
					t.Error("invalid OTLP HTTP request")
				}
				body, _ := io.ReadAll(r.Body)
				var msg collectortrace.ExportTraceServiceRequest
				if err := proto.Unmarshal(body, &msg); err != nil {
					t.Error(err)
				}
				requests <- &msg
				w.Header().Set("Content-Type", "application/x-protobuf")
			}))
			defer collector.Close()
			exp, err := otlptracehttp.New(context.Background(), otlptracehttp.WithEndpointURL(collector.URL+"/tracer/v1/traces"), otlptracehttp.WithCompression(otlptracehttp.NoCompression))
			if err != nil {
				t.Fatal(err)
			}
			tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp), sdktrace.WithResource(resource.NewSchemaless(attribute.String("project_name", "offline-openai"))))
			defer tp.Shutdown(context.Background())
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("X-Api-Key") != "" || r.Header.Get("X-Secret-Key") != "" {
					t.Error("collector credentials sent to model")
				}
				w.Header().Set("Content-Type", "application/json")
				if strings.HasSuffix(r.URL.Path, "/embeddings") {
					_, _ = io.WriteString(w, embeddingJSON)
					return
				}
				if r.Header.Get("X-Test-Stream") != "" {
					w.Header().Set("Content-Type", "text/event-stream")
					writeEvent(w, `{"type":"response.completed","response":`+responseJSON+`}`)
					return
				}
				_, _ = io.WriteString(w, responseJSON)
			}))
			defer server.Close()
			client := openai.NewClient(option.WithBaseURL(server.URL+"/v1/"), option.WithAPIKey(testOpenAIKey), option.WithMaxRetries(0), option.WithMiddleware(traceaiopenai.Middleware(traceaiopenai.WithTracerProvider(tp), traceaiopenai.WithTraceConfig(traceaiopenai.TraceConfig{HideInputs: hidden, HideOutputs: hidden}))))
			if _, err := client.Responses.New(context.Background(), responseParams()); err != nil {
				t.Fatal(err)
			}
			stream := client.Responses.NewStreaming(context.Background(), responseParams(), option.WithHeader("X-Test-Stream", "true"))
			for stream.Next() {
			}
			if stream.Err() != nil {
				t.Fatal(stream.Err())
			}
			_ = stream.Close()
			if _, err := client.Embeddings.New(context.Background(), openai.EmbeddingNewParams{Model: "text-embedding-3-small", Input: openai.EmbeddingNewParamsInputUnion{OfString: openai.String(marker)}, EncodingFormat: openai.EmbeddingNewParamsEncodingFormatFloat}); err != nil {
				t.Fatal(err)
			}
			if len(requests) != 3 {
				t.Fatalf("wire exports = %d, want 3", len(requests))
			}
			for i := 0; i < 3; i++ {
				msg := <-requests
				wire, _ := protojson.Marshal(msg)
				if strings.Contains(string(wire), testOpenAIKey) || strings.Contains(string(wire), "0.123456789") || (hidden && strings.Contains(string(wire), marker)) {
					t.Errorf("sensitive content on OTLP wire: %s", wire)
				}
				if !hidden && !strings.Contains(string(wire), marker) {
					t.Error("capture-enabled content missing on wire")
				}
				if len(msg.ResourceSpans) != 1 || len(msg.ResourceSpans[0].ScopeSpans) != 1 || len(msg.ResourceSpans[0].ScopeSpans[0].Spans) != 1 {
					t.Fatal("unexpected span shape")
				}
				scope := msg.ResourceSpans[0].ScopeSpans[0]
				s := scope.Spans[0]
				if scope.Scope.Name != traceaiopenai.ScopeName || s.Kind != tracepb.Span_SPAN_KIND_CLIENT || len(s.TraceId) != 16 || len(s.SpanId) != 8 {
					t.Error("invalid OTLP span identity")
				}
				got := map[string]string{}
				for _, kv := range s.Attributes {
					got[kv.Key] = kv.Value.GetStringValue()
				}
				want := "LLM"
				if i == 2 {
					want = "EMBEDDING"
				}
				if got[traceaiopenai.GenAISpanKind] != want || got[traceaiopenai.GenAIProviderName] != "openai" {
					t.Error("wire attribution/type incorrect")
				}
			}
		})
	}
}
