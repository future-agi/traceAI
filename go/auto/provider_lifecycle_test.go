package auto

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"path/filepath"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func ownedRegressionSetup(t *testing.T) {
	t.Helper()
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	otel.SetTracerProvider(trace.NewNoopTracerProvider())
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	t.Cleanup(func() { _ = Shutdown(context.Background()); otel.SetTracerProvider(old) })
}

func TestGlobalProviderRetirement(t *testing.T) {
	ownedRegressionSetup(t)
	first := tracetest.NewSpanRecorder()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(first)); err != nil {
		t.Fatal(err)
	}
	p := agent.current.provider
	otel.SetTracerProvider(p)
	q := sdktrace.NewTracerProvider()
	defer q.Shutdown(context.Background())
	if _, err := InitForTest(context.Background(), WithTracerProvider(q), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
		t.Fatal(err)
	}
	rec := tracetest.NewSpanRecorder()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
		t.Fatal(err)
	}
	if agent.current.provider != p {
		t.Fatal("second init replaced global provider")
	}
	_, span := otel.Tracer("library").Start(context.Background(), "after-retirement")
	if !span.IsRecording() {
		t.Fatal("new global provider not recording")
	}
	span.End()
	if len(first.Ended()) != 1 || len(rec.Ended()) != 0 {
		t.Fatalf("second init rewired original provider: %d", len(rec.Ended()))
	}
}

func TestProjectResourceReconfiguration(t *testing.T) {
	ownedRegressionSetup(t)
	first, second := tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(first), WithProject("first", "observe")); err != nil {
		t.Fatal(err)
	}
	p := agent.current.provider
	otel.SetTracerProvider(p)
	cached := p.Tracer("library-cached")
	_, old := cached.Start(context.Background(), "old-project")
	old.End()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(second), WithProject("second", "experiment")); err != nil {
		t.Fatal(err)
	}
	_, span := cached.Start(context.Background(), "new-project")
	span.End()
	if agent.current.provider != p || Report().Config.ProjectName != "first" || len(first.Ended()) != 2 || len(second.Ended()) != 0 {
		t.Fatal("reconfiguration lost provider identity or export")
	}
	for _, tc := range []struct {
		span       sdktrace.ReadOnlySpan
		name, kind string
	}{{first.Ended()[0], "first", "observe"}, {first.Ended()[1], "first", "observe"}} {
		attrs := map[string]string{}
		for _, kv := range tc.span.Resource().Attributes() {
			attrs[string(kv.Key)] = kv.Value.AsString()
		}
		if attrs["project_name"] != tc.name || attrs["project_type"] != tc.kind {
			t.Fatalf("stale resource: %v", attrs)
		}
	}
}

func TestFailedInitializationReports(t *testing.T) {
	ownedRegressionSetup(t)
	for _, sample := range []string{"NaN", "+Inf", "-Inf", "invalid", "2"} {
		t.Run(sample, func(t *testing.T) {
			t.Setenv("FI_SAMPLE_RATE", sample)
			if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err == nil {
				t.Fatal("invalid sample accepted")
			}
			r := Report()
			b, err := json.Marshal(r)
			if err != nil || len(b) == 0 || len(r.Integrations) == 0 || r.Integrations[0].Status != "failed" {
				t.Fatalf("failed report not JSON-safe: %+v %v", r, err)
			}
			if math.IsNaN(r.Config.SampleRate) || math.IsInf(r.Config.SampleRate, 0) {
				t.Fatal("nonfinite sample in report")
			}
		})
	}
	// A rejected replacement must report the attempted config without stopping
	// the previously valid instrumentation.
	if _, err := InitForTest(context.Background(), WithProject("valid", "observe"), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
		t.Fatal(err)
	}
	previous := agent.current
	if _, err := InitForTest(context.Background(), WithProject("attempt", "invalid-type"), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
		t.Fatal("second init evaluated ignored options")
	}
	r := Report()
	if agent.current != previous || !previous.active.Load() || r.Config.ProjectType != "observe" || r.Integrations[0].Status != "instrumented" {
		t.Fatalf("stale failed project diagnostic: %+v", r)
	}
}

func TestLibraryExportTimePrivacy(t *testing.T) {
	for _, tc := range []struct {
		name string
		cfg  TraceConfig
	}{
		{"scrub", TraceConfig{}},
		{"inputs", TraceConfig{HideInputs: true}},
		{"outputs", TraceConfig{HideOutputs: true}},
		{"input-text", TraceConfig{HideInputText: true}},
		{"output-text", TraceConfig{HideOutputText: true}},
		{"input-messages", TraceConfig{HideInputMessages: true}},
		{"output-messages", TraceConfig{HideOutputMessages: true}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ownedRegressionSetup(t)
			app := tracetest.NewSpanRecorder()
			p := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(app))
			defer p.Shutdown(context.Background())
			rec := tracetest.NewSpanRecorder()
			if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithSpanProcessor(rec), WithCredentials("FI_API_PRIVATE", "FI_SECRET_PRIVATE"), WithTraceConfig(tc.cfg)); err != nil {
				t.Fatal(err)
			}
			secret := `{"authorization_token":"JSON_PRIVATE","url":"https://example.com?sig=SIGNED_PRIVATE","image":"data:image/png;base64,QUJDQUJD","payload":{"bytes":"` + strings.Repeat("YWJj", 100) + `"},"echo":"FI_API_PRIVATE FI_SECRET_PRIVATE"}`
			_, span := p.Tracer("traceai-library").Start(context.Background(), "library-span", trace.WithAttributes(
				attribute.String("gen_ai.span.kind", "LLM"), attribute.String("gen_ai.operation.name", "chat"),
				attribute.String("input.value", "INPUT_PRIVATE"), attribute.String("output.value", "OUTPUT_PRIVATE"),
				attribute.String("gen_ai.system_instructions", "INPUT_PRIVATE"), attribute.String("gen_ai.tool.call.arguments", "INPUT_PRIVATE"), attribute.String("gen_ai.tool.call.result", "OUTPUT_PRIVATE"),
				attribute.String("metadata", secret), attribute.StringSlice("metadata.list", []string{secret}), attribute.String("api_key", "ATTR_PRIVATE"), attribute.String("inline.data", strings.Repeat("YWJj", 100)),
			), trace.WithLinks(trace.Link{SpanContext: trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{2}}), Attributes: []attribute.KeyValue{attribute.String("metadata", secret), attribute.String("input.value", "INPUT_PRIVATE"), attribute.String("output.value", "OUTPUT_PRIVATE")}}))
			span.SetStatus(codes.Error, "FI_API_PRIVATE "+secret+" STATUS_PRIVATE")
			span.RecordError(errors.New(secret+" EVENT_PRIVATE"), trace.WithStackTrace(true))
			span.AddEvent("exception", trace.WithAttributes(attribute.StringSlice("exception.message", []string{secret + " EVENT_PRIVATE"}), attribute.String("exception.stacktrace", "FI_SECRET_PRIVATE EVENT_PRIVATE"), attribute.String("exception.type", "CustomError")))
			span.End()
			if len(rec.Ended()) != 1 || len(app.Ended()) != 1 {
				t.Fatal("missing exported library span")
			}
			s := rec.Ended()[0]
			var all strings.Builder
			for _, kv := range s.Attributes() {
				all.WriteString(kv.Value.Emit())
			}
			for _, link := range s.Links() {
				for _, kv := range link.Attributes {
					v := kv.Value.Emit()
					all.WriteString(v)
					if (tc.cfg.HideInputs || tc.cfg.HideInputText || tc.cfg.HideInputMessages) && strings.Contains(v, "INPUT_PRIVATE") {
						t.Fatal("hidden input in link attribute")
					}
					if (tc.cfg.HideOutputs || tc.cfg.HideOutputText || tc.cfg.HideOutputMessages) && strings.Contains(v, "OUTPUT_PRIVATE") {
						t.Fatal("hidden output in link attribute")
					}
				}
			}
			all.WriteString(s.Status().Description)
			for _, event := range s.Events() {
				for _, kv := range event.Attributes {
					all.WriteString(kv.Value.Emit())
				}
			}
			for _, forbidden := range []string{"FI_API_PRIVATE", "FI_SECRET_PRIVATE", "JSON_PRIVATE", "SIGNED_PRIVATE", "QUJDQUJD", "YWJj", "ATTR_PRIVATE"} {
				if strings.Contains(all.String(), forbidden) {
					t.Fatalf("library export leaked %s", forbidden)
				}
			}
			if hidesContent(tc.cfg) {
				if s.Status().Description != "" {
					t.Fatal("hidden status text leaked")
				}
				for _, e := range s.Events() {
					for _, kv := range e.Attributes {
						if strings.Contains(kv.Value.Emit(), "EVENT_PRIVATE") {
							t.Fatal("hidden exception text leaked")
						}
					}
				}
			}
			for _, kv := range s.Attributes() {
				v := kv.Value.Emit()
				if (tc.cfg.HideInputs || tc.cfg.HideInputText || tc.cfg.HideInputMessages) && strings.Contains(v, "INPUT_PRIVATE") {
					t.Fatalf("hidden input in %s", kv.Key)
				}
				if (tc.cfg.HideOutputs || tc.cfg.HideOutputText || tc.cfg.HideOutputMessages) && strings.Contains(v, "OUTPUT_PRIVATE") {
					t.Fatalf("hidden output in %s", kv.Key)
				}
			}
			if !strings.Contains(app.Ended()[0].Status().Description, "FI_API_PRIVATE") || !strings.Contains(app.Ended()[0].Events()[0].Attributes[1].Value.Emit(), "EVENT_PRIVATE") {
				t.Fatal("agent mutated app-owned processor snapshot")
			}
		})
	}
}
