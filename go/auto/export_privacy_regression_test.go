package auto_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func TestLibraryRequestCredentialEchoPrivacy(t *testing.T) {
	rec, p := regressionSetup(t, auto.TraceConfig{})
	ctx, span := p.Tracer("traceai-library").Start(context.Background(), "library", trace.WithAttributes(attribute.String("gen_ai.span.kind", "LLM")))
	regressionExchange(t, regressionTransport(responseJSON, "application/json", 200), ctx, "https://api.openai.com/v1/chat/completions", requestJSON, http.Header{"Authorization": {"Bearer PROVIDER_HEADER_PRIVATE"}})
	span.SetAttributes(attribute.String("input.value", "PROVIDER_HEADER_PRIVATE"))
	span.SetStatus(codes.Error, "failure PROVIDER_HEADER_PRIVATE")
	span.RecordError(errors.New("failure PROVIDER_HEADER_PRIVATE"))
	span.End()
	if len(wires(rec)) != 0 || len(rec.Ended()) != 1 {
		t.Fatal("library deduplication changed")
	}
	s := rec.Ended()[0]
	if value(s, "input.value") != "<redacted>" || s.Status().Description != "failure <redacted>" {
		t.Fatal("library HTTP credential echo exported")
	}
	for _, event := range s.Events() {
		for _, kv := range event.Attributes {
			if strings.Contains(kv.Value.Emit(), "PROVIDER_HEADER_PRIVATE") {
				t.Fatal("library exception leaked provider header")
			}
		}
	}
}

type latePrivacyProcessor struct{ *tracetest.SpanRecorder }

func (p *latePrivacyProcessor) OnStart(ctx context.Context, span sdktrace.ReadWriteSpan) {
	p.SpanRecorder.OnStart(ctx, span)
	span.SetAttributes(attribute.String("metadata.late", "PROVIDER_HEADER_PRIVATE"), attribute.String("gen_ai.system_instructions", "LATE_INPUT_PRIVATE"), attribute.String("gen_ai.tool.call.result", "LATE_OUTPUT_PRIVATE"))
	span.AddEvent("exception", trace.WithAttributes(attribute.StringSlice("exception.message", []string{`{"api_key":"JSON_PRIVATE"}`, "PROVIDER_HEADER_PRIVATE EVENT_PRIVATE"})))
}
func TestWireExportTimePrivacy(t *testing.T) {
	for _, hidden := range []bool{false, true} {
		t.Run(map[bool]string{false: "scrub", true: "hide"}[hidden], func(t *testing.T) {
			rec := tracetest.NewSpanRecorder()
			processor := &latePrivacyProcessor{rec}
			_, _ = setup(t, auto.WithSpanProcessor(processor), auto.WithTraceConfig(auto.TraceConfig{HideInputs: hidden, HideOutputs: hidden}))
			tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
				_, _ = io.Copy(io.Discard, r.Body)
				_ = r.Body.Close()
				return &http.Response{StatusCode: 400, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"error":{"message":"failure PROVIDER_HEADER_PRIVATE"}}`))}, nil
			}))
			regressionExchange(t, tr, context.Background(), "https://api.openai.com/v1/chat/completions", requestJSON, http.Header{"Authorization": {"Bearer PROVIDER_HEADER_PRIVATE"}})
			ss := wires(rec)
			if len(ss) != 1 {
				t.Fatal("missing wire span")
			}
			s := ss[0]
			if value(s, "metadata.late") != "<redacted>" {
				t.Fatal("late processor attribute leaked header")
			}
			if strings.Contains(s.Status().Description, "PROVIDER_HEADER_PRIVATE") || hidden && s.Status().Description != "" {
				t.Fatal("wire status leaked")
			}
			for _, event := range s.Events() {
				for _, kv := range event.Attributes {
					v := kv.Value.Emit()
					if strings.Contains(v, "JSON_PRIVATE") || strings.Contains(v, "PROVIDER_HEADER_PRIVATE") || hidden && strings.Contains(v, "EVENT_PRIVATE") {
						t.Fatal("wire event leaked")
					}
				}
			}
			if hidden {
				for _, kv := range s.Attributes() {
					if strings.Contains(kv.Value.Emit(), "LATE_INPUT_PRIVATE") || strings.Contains(kv.Value.Emit(), "LATE_OUTPUT_PRIVATE") {
						t.Fatal("late content bypassed masking")
					}
				}
			}
		})
	}
}
