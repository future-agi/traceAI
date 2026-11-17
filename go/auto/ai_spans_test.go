package auto

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

// With an application-owned provider, Future AGI receives only AI spans: the
// agent's wire spans and spans that carry gen_ai.span.kind or fi.span.kind
// (traceAI modules). Any other application span, here one with a
// credential-named attribute, still reaches the application's own exporters.
func TestFIExporterReceivesOnlyAISpans(t *testing.T) {
	ownedRegressionSetup(t)
	received := make(chan *collector.ExportTraceServiceRequest, 8)
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		request := &collector.ExportTraceServiceRequest{}
		if err := proto.Unmarshal(data, request); err != nil {
			t.Error(err)
		}
		received <- request
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	defer sink.Close()
	t.Setenv("FI_BASE_URL", sink.URL)
	app := tracetest.NewSpanRecorder()
	p := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(app))
	defer p.Shutdown(context.Background())
	if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("AI_ONLY_FI_KEY", "AI_ONLY_FI_SECRET")); err != nil {
		t.Fatal(err)
	}
	tracer := p.Tracer("application")
	ctx, request := tracer.Start(context.Background(), "application-request", trace.WithAttributes(attribute.String("app.api_key", "sk-test-PLACEHOLDER")))
	_, module := tracer.Start(ctx, "module-step", trace.WithAttributes(attribute.String("gen_ai.span.kind", "CHAIN")))
	module.End()
	_, step := tracer.Start(ctx, "fi-step", trace.WithAttributes(attribute.String("fi.span.kind", "TOOL")))
	step.End()
	r, _ := http.NewRequestWithContext(ctx, "POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
	resp, err := Transport(round5SuccessTransport()).RoundTrip(r)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	request.End()
	if err := ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	traceID, spanID := request.SpanContext().TraceID(), request.SpanContext().SpanID()
	names := map[string]bool{}
	wires := 0
	var data []byte
	for done := false; !done; {
		select {
		case export := <-received:
			raw, _ := proto.Marshal(export)
			data = append(data, raw...)
			for _, rs := range export.ResourceSpans {
				for _, ss := range rs.ScopeSpans {
					for _, s := range ss.Spans {
						names[s.Name] = true
						for _, kv := range s.Attributes {
							if kv.Key == "traceai.capture.layer" && kv.Value.GetStringValue() == "wire" {
								wires++
								if !bytes.Equal(s.TraceId, traceID[:]) || !bytes.Equal(s.ParentSpanId, spanID[:]) {
									t.Fatal("the wire span lost its application parent")
								}
							}
						}
					}
				}
			}
		default:
			done = true
		}
	}
	if names["application-request"] || bytes.Contains(data, []byte("sk-test-PLACEHOLDER")) {
		t.Fatalf("an ordinary application span reached the FI exporter: %v", names)
	}
	if !names["module-step"] || !names["fi-step"] || wires != 1 {
		t.Fatalf("AI spans must still reach the FI exporter: %v, wire spans %d", names, wires)
	}
	if len(app.Ended()) != 4 {
		t.Fatalf("the application's own exporter lost spans: %d", len(app.Ended()))
	}
}
