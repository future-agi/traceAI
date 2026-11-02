package traceaigenkit_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/firebase/genkit/go/ai"
	"github.com/firebase/genkit/go/core/tracing"
	"github.com/firebase/genkit/go/genkit"
	"github.com/firebase/genkit/go/plugins/otelgenai"
	traceaigenkit "github.com/future-agi/traceAI/go/genkit"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

const secret = "PRIVATE_STOP_SEQUENCE_SYNTHETIC_7824"

type sink struct{ spans []sdktrace.ReadOnlySpan }

func (s *sink) ExportSpans(_ context.Context, spans []sdktrace.ReadOnlySpan) error {
	s.spans = append(s.spans, spans...)
	return nil
}
func (*sink) Shutdown(context.Context) error { return nil }
func TestHideInputsDoesNotExportStopSequence(t *testing.T) {
	c := &sink{}
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(traceaigenkit.NewExporter(c, traceaigenkit.Config{HideInputs: true, HideOutputs: true, HideErrorDetails: true})))
	tracing.SetInstrumentation(otelgenai.New(&otelgenai.Options{TracerProvider: tp, ContentCapturingMode: otelgenai.NoContent, CaptureActionIO: false, DisableMetrics: true}))
	defer tracing.SetInstrumentation()
	g := genkit.Init(context.Background())
	genkit.DefineModel(g, "test/stop", &ai.ModelOptions{Supports: &ai.ModelSupports{Multiturn: true}}, func(_ context.Context, req *ai.ModelRequest, _ ai.ModelStreamCallback) (*ai.ModelResponse, error) {
		return &ai.ModelResponse{Request: req, Message: ai.NewModelTextMessage("okay"), FinishReason: ai.FinishReasonStop}, nil
	})
	_, err := genkit.Generate(context.Background(), g, ai.WithModelName("test/stop"), ai.WithPrompt("prompt"), ai.WithConfig(&ai.GenerationCommonConfig{StopSequences: []string{secret}}))
	if err != nil {
		t.Fatal(err)
	}
	if err := tp.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, sp := range c.spans {
		for _, attr := range sp.Attributes() {
			if strings.Contains(attr.Value.Emit(), secret) {
				t.Errorf("leaked %s on %s", attr.Key, sp.Name())
			}
		}
	}
}
func TestHideInputsDefaultsToHidingErrorDetails(t *testing.T) {
	t.Setenv("FI_HIDE_INPUTS", "true")
	t.Setenv("FI_HIDE_OUTPUTS", "true")
	t.Setenv("FI_HIDE_ERROR_DETAILS", "")
	if conf := traceaigenkit.ConfigFromEnv(); !conf.HideErrorDetails {
		t.Errorf("FI_HIDE_ERROR_DETAILS unset while content flags enabled: %#v", conf)
	}
	t.Setenv("FI_HIDE_ERROR_DETAILS", "false")
	if conf := traceaigenkit.ConfigFromEnv(); conf.HideErrorDetails {
		t.Errorf("explicit false must override content default: %#v", conf)
	}
	t.Setenv("FI_HIDE_ERROR_DETAILS", " TrUe ")
	if conf := traceaigenkit.ConfigFromEnv(); !conf.HideErrorDetails {
		t.Errorf("explicit true must enable error privacy: %#v", conf)
	}
}
func TestNativeErrorQuotedPromptHiddenWhenErrorSwitchUnset(t *testing.T) {
	t.Setenv("FI_HIDE_INPUTS", "true")
	t.Setenv("FI_HIDE_OUTPUTS", "true")
	t.Setenv("FI_HIDE_ERROR_DETAILS", "")
	c := &sink{}
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(traceaigenkit.NewExporter(c, traceaigenkit.ConfigFromEnv())))
	tracing.SetInstrumentation(otelgenai.New(&otelgenai.Options{TracerProvider: tp, ContentCapturingMode: otelgenai.NoContent, CaptureActionIO: false, DisableMetrics: true}))
	defer tracing.SetInstrumentation()
	g := genkit.Init(context.Background())
	genkit.DefineModel(g, "test/errors", &ai.ModelOptions{Supports: &ai.ModelSupports{Multiturn: true}}, func(_ context.Context, req *ai.ModelRequest, _ ai.ModelStreamCallback) (*ai.ModelResponse, error) {
		return nil, errors.New("provider rejected input: " + secret)
	})
	_, err := genkit.Generate(context.Background(), g, ai.WithModelName("test/errors"), ai.WithPrompt(secret))
	if err == nil {
		t.Fatal("missing application error")
	}
	if err := tp.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, sp := range c.spans {
		if strings.Contains(sp.Status().Description, secret) {
			t.Errorf("input exposed in status on %s", sp.Name())
		}
		for _, event := range sp.Events() {
			for _, a := range event.Attributes {
				if strings.Contains(a.Value.Emit(), secret) {
					t.Errorf("input exposed in %s event on %s", a.Key, sp.Name())
				}
			}
		}
	}
}
func TestOutputPrivacyPreservesNonContentOutputType(t *testing.T) {
	c := &sink{}
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(traceaigenkit.NewExporter(c, traceaigenkit.Config{HideOutputs: true})))
	tracing.SetInstrumentation(otelgenai.New(&otelgenai.Options{TracerProvider: tp, ContentCapturingMode: otelgenai.NoContent, CaptureActionIO: false, DisableMetrics: true}))
	defer tracing.SetInstrumentation()
	g := genkit.Init(context.Background())
	genkit.DefineModel(g, "test/format", &ai.ModelOptions{Supports: &ai.ModelSupports{Multiturn: true}}, func(_ context.Context, req *ai.ModelRequest, _ ai.ModelStreamCallback) (*ai.ModelResponse, error) {
		return &ai.ModelResponse{Request: req, Message: ai.NewModelTextMessage(`{"item":"soup"}`), FinishReason: ai.FinishReasonStop}, nil
	})
	_, err := genkit.Generate(context.Background(), g, ai.WithModelName("test/format"), ai.WithPrompt("menu"), ai.WithOutputFormat("json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := tp.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, sp := range c.spans {
		for _, a := range sp.Attributes() {
			if a.Key == "gen_ai.output.type" {
				found = true
				if got := a.Value.AsString(); got != "json" {
					t.Errorf("output format changed: %q", got)
				}
			}
		}
	}
	if !found {
		t.Error("Genkit output.type metadata lost with FI_HIDE_OUTPUTS=true")
	}
}
