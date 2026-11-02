package traceaigenkit

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/firebase/genkit/go/ai"
	"github.com/firebase/genkit/go/core/tracing"
	"github.com/firebase/genkit/go/genkit"
	"github.com/firebase/genkit/go/plugins/otelgenai"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

const (
	promptSecret = "PROMPT_PRIVATE_SYNTHETIC_4801"
	answerSecret = "RESPONSE_PRIVATE_SYNTHETIC_4802"
	errorSecret  = "ERROR_PRIVATE_SYNTHETIC_4803"
)

type capture struct {
	mu    sync.Mutex
	spans []sdktrace.ReadOnlySpan
}

func (c *capture) ExportSpans(_ context.Context, spans []sdktrace.ReadOnlySpan) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.spans = append(c.spans, spans...)
	return nil
}
func (*capture) Shutdown(context.Context) error { return nil }
func (c *capture) snapshot() []sdktrace.ReadOnlySpan {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]sdktrace.ReadOnlySpan(nil), c.spans...)
}

func TestGenkitPrivacyOnRealSpans(t *testing.T) {
	for _, mode := range []otelgenai.ContentCapturingMode{otelgenai.NoContent, otelgenai.SpanOnly} {
		t.Run(fmt.Sprint(mode), func(t *testing.T) {
			t.Setenv("FI_HIDE_INPUTS", "true")
			t.Setenv("FI_HIDE_OUTPUTS", "true")
			t.Setenv("FI_HIDE_ERROR_DETAILS", "true")
			sink := &capture{}
			tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(NewExporter(sink, ConfigFromEnv())))
			tracing.SetInstrumentation(otelgenai.New(&otelgenai.Options{TracerProvider: tp, ContentCapturingMode: mode, CaptureActionIO: false, DisableMetrics: true}))
			t.Cleanup(func() { tracing.SetInstrumentation(); _ = tp.Shutdown(context.Background()) })
			g := genkit.Init(context.Background())
			genkit.DefineModel(g, "test/private", &ai.ModelOptions{Supports: &ai.ModelSupports{Multiturn: true}}, func(_ context.Context, req *ai.ModelRequest, _ ai.ModelStreamCallback) (*ai.ModelResponse, error) {
				if req.Messages[len(req.Messages)-1].Text() == "error" {
					return nil, errors.New(errorSecret)
				}
				return &ai.ModelResponse{Request: req, Message: ai.NewModelTextMessage(answerSecret), FinishReason: ai.FinishReasonStop, Usage: &ai.GenerationUsage{InputTokens: 3, OutputTokens: 2}}, nil
			})
			r, err := genkit.Generate(context.Background(), g, ai.WithModelName("test/private"), ai.WithPrompt(promptSecret))
			if err != nil || r.Text() != answerSecret {
				t.Fatalf("result changed: %v %v", r, err)
			}
			_, err = genkit.Generate(context.Background(), g, ai.WithModelName("test/private"), ai.WithPrompt("error"))
			if err == nil || !strings.Contains(err.Error(), errorSecret) {
				t.Fatalf("application error changed: %v", err)
			}
			spans := sink.snapshot()
			if len(spans) < 4 {
				t.Fatalf("missing native spans: %d", len(spans))
			}
			models := 0
			errors := 0
			for _, span := range spans {
				if strings.Contains(span.Name(), "chat private") {
					models++
					for _, a := range span.Attributes() {
						if a.Key == attribute.Key("gen_ai.usage.input_tokens") && a.Value.AsInt64() != 3 {
							t.Fatal("usage changed")
						}
					}
				}
				if span.Status().Code == codes.Error {
					errors++
				}
				if strings.Contains(span.Status().Description, errorSecret) {
					t.Errorf("unredacted status on %s", span.Name())
				}
				for _, a := range span.Attributes() {
					if strings.Contains(a.Value.Emit(), promptSecret) || strings.Contains(a.Value.Emit(), answerSecret) || strings.Contains(a.Value.Emit(), errorSecret) {
						t.Errorf("unredacted attribute %s", a.Key)
					}
				}
				for _, event := range span.Events() {
					for _, a := range event.Attributes {
						if strings.Contains(a.Value.Emit(), errorSecret) {
							t.Errorf("unredacted event %s on %s", a.Key, span.Name())
						}
					}
				}
			}
			if models != 2 || errors == 0 {
				t.Errorf("native model/error spans lost: models=%d errors=%d", models, errors)
			}
		})
	}
}

func TestIndependentPrivacySwitches(t *testing.T) {
	source := &capture{}
	cases := []struct {
		name                     string
		config                   Config
		keepIn, keepOut, keepErr bool
	}{
		{"none", Config{}, true, true, true},
		{"inputs", Config{HideInputs: true}, false, true, true},
		{"outputs", Config{HideOutputs: true}, true, false, true},
		{"errors", Config{HideErrorDetails: true}, true, true, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			source.spans = nil
			tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(NewExporter(source, tc.config)))
			_, s := tp.Tracer("test").Start(context.Background(), "test")
			s.SetAttributes(attribute.String("gen_ai.input.messages", promptSecret), attribute.String("gen_ai.output.messages", answerSecret))
			s.RecordError(errors.New(errorSecret))
			s.SetStatus(codes.Error, errorSecret)
			s.End()
			_ = tp.Shutdown(context.Background())
			spans := source.snapshot()
			if len(spans) != 1 {
				t.Fatalf("spans=%d", len(spans))
			}
			attrs := map[string]string{}
			for _, a := range spans[0].Attributes() {
				attrs[string(a.Key)] = a.Value.AsString()
			}
			if (attrs["gen_ai.input.messages"] == promptSecret) != tc.keepIn || (attrs["gen_ai.output.messages"] == answerSecret) != tc.keepOut {
				t.Errorf("content switches: %v", attrs)
			}
			if strings.Contains(spans[0].Status().Description, errorSecret) != tc.keepErr {
				t.Error("status switch")
			}
			for _, event := range spans[0].Events() {
				for _, a := range event.Attributes {
					if a.Key == "exception.message" && (a.Value.AsString() == errorSecret) != tc.keepErr {
						t.Error("exception switch")
					}
				}
			}
		})
	}
}
