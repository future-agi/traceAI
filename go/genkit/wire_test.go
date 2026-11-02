package traceaigenkit

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/firebase/genkit/go/ai"
	"github.com/firebase/genkit/go/core/tracing"
	"github.com/firebase/genkit/go/genkit"
	"github.com/firebase/genkit/go/plugins/otelgenai"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	tracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

func TestNativeGenkitOTLPWirePrivacy(t *testing.T) {
	var mu sync.Mutex
	var packets [][]byte
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			w.WriteHeader(500)
			return
		}
		mu.Lock()
		packets = append(packets, body)
		mu.Unlock()
	}))
	defer receiver.Close()
	exporter, err := otlptracehttp.New(context.Background(), otlptracehttp.WithEndpointURL(receiver.URL+"/tracer/v1/traces"), otlptracehttp.WithCompression(otlptracehttp.NoCompression))
	if err != nil {
		t.Fatal(err)
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(NewExporter(exporter, Config{HideInputs: true, HideOutputs: true, HideErrorDetails: true})))
	tracing.SetInstrumentation(otelgenai.New(&otelgenai.Options{TracerProvider: tp, ContentCapturingMode: otelgenai.SpanOnly, CaptureActionIO: false, DisableMetrics: true}))
	defer tracing.SetInstrumentation()
	g := genkit.Init(context.Background())
	genkit.DefineModel(g, "test/private", &ai.ModelOptions{Supports: &ai.ModelSupports{Multiturn: true}}, func(_ context.Context, req *ai.ModelRequest, _ ai.ModelStreamCallback) (*ai.ModelResponse, error) {
		if req.Messages[len(req.Messages)-1].Text() == "error" {
			return nil, errors.New(errorSecret)
		}
		return &ai.ModelResponse{Request: req, Message: ai.NewModelTextMessage(answerSecret), FinishReason: ai.FinishReasonStop, Usage: &ai.GenerationUsage{InputTokens: 3, OutputTokens: 2}}, nil
	})
	if _, err := genkit.Generate(context.Background(), g, ai.WithModelName("test/private"), ai.WithPrompt(promptSecret)); err != nil {
		t.Fatal(err)
	}
	if _, err := genkit.Generate(context.Background(), g, ai.WithModelName("test/private"), ai.WithPrompt("error")); err == nil {
		t.Fatal("model error lost")
	}
	if err := tp.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(packets) == 0 {
		t.Fatal("no OTLP export")
	}
	modelCount := 0
	errCount := 0
	for _, payload := range packets {
		for _, secret := range []string{promptSecret, answerSecret, errorSecret} {
			if bytes.Contains(payload, []byte(secret)) {
				t.Errorf("secret in OTLP trace: %s", secret)
			}
		}
		var decoded tracepb.ExportTraceServiceRequest
		if err := proto.Unmarshal(payload, &decoded); err != nil {
			t.Fatal(err)
		}
		for _, rs := range decoded.ResourceSpans {
			for _, ss := range rs.ScopeSpans {
				for _, span := range ss.Spans {
					if span.Name == "chat private" {
						modelCount++
					}
					if span.Status.GetCode() == 2 {
						errCount++
						if span.Status.GetMessage() != "" {
							t.Error("error status description leaked")
						}
					}
				}
			}
		}
	}
	if modelCount != 2 || errCount == 0 {
		t.Errorf("native span identity/status lost: model=%d error=%d", modelCount, errCount)
	}
}
