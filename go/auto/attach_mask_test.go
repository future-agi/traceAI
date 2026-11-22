package auto

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// both activates the agent on an application-owned provider with a recorder for the application's own
// exporter and a custom processor standing in for the Future AGI route.
func both(t *testing.T, env map[string]string) (app, fi *tracetest.SpanRecorder) {
	t.Helper()
	moduleSetup(t)
	for k, v := range env {
		t.Setenv(k, v)
	}
	app, fi = tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(app))
	t.Cleanup(func() { _ = provider.Shutdown(context.Background()) })
	if _, err := InitForTest(context.Background(), WithTracerProvider(provider), WithSpanProcessor(fi)); err != nil {
		t.Fatal(err)
	}
	return app, fi
}

func einoTracer() trace.Tracer { return Attach("eino").TracerProvider().Tracer("module") }

func one(t *testing.T, rec *tracetest.SpanRecorder) sdktrace.ReadOnlySpan {
	t.Helper()
	ended := rec.Ended()
	if len(ended) != 1 {
		t.Fatalf("received %d spans", len(ended))
	}
	return ended[0]
}

func dump(s sdktrace.ReadOnlySpan) string {
	return fmt.Sprint(s.Name(), s.Attributes(), s.Events(), s.Links(), s.Status().Description)
}

// A module that reads the span back from the context it was handed writes through the same masking.
func TestModuleContextCarriesTheMaskedSpan(t *testing.T) {
	app, _ := both(t, map[string]string{"FI_HIDE_INPUTS": "true"})
	ctx, span := einoTracer().Start(context.Background(), "chain")
	trace.SpanFromContext(ctx).SetAttributes(attribute.String("input.value", "prompt text from the context"))
	trace.SpanFromContext(ctx).AddEvent("noted", trace.WithAttributes(attribute.String("note", "event text from the context")))
	span.End()
	if got := dump(one(t, app)); strings.Contains(got, "from the context") {
		t.Fatalf("content written through SpanFromContext reached the application's exporter: %s", got)
	}
}

// A rename and a late link follow the rules a span's start follows: the same name gives the same result either way.
func TestModuleRenameAndLateLinkAreMasked(t *testing.T) {
	app, _ := both(t, map[string]string{"FI_HIDE_INPUTS": "true", "FI_HIDE_INPUT_IMAGES": "true"})
	name := "step " + imageJSON + " https://img.example.test/a.png data:image/png;base64,QUJDREVGR0hJSktMTU5PUA=="
	_, started := einoTracer().Start(context.Background(), name)
	started.End()
	_, span := einoTracer().Start(context.Background(), "chain")
	span.SetName(name)
	linked := trace.SpanContext{}.WithTraceID(trace.TraceID{2}).WithSpanID(trace.SpanID{2}).WithTraceFlags(trace.FlagsSampled)
	span.AddLink(trace.Link{SpanContext: linked, Attributes: []attribute.KeyValue{attribute.String("input.value", "late link text")}})
	span.End()
	ended := app.Ended()
	if len(ended) != 2 {
		t.Fatalf("received %d spans", len(ended))
	}
	if ended[0].Name() != ended[1].Name() {
		t.Errorf("a rename gives %q, the same name at start gives %q", ended[1].Name(), ended[0].Name())
	}
	if got := fmt.Sprint(ended[1].Links()); strings.Contains(got, "late link text") {
		t.Errorf("a late link's attributes reached the application's exporter: %s", got)
	}
}

// While a content switch is on, an event value of any type except exception.type is replaced.
func TestModuleEventRedactsEveryValueType(t *testing.T) {
	app, _ := both(t, map[string]string{"FI_HIDE_INPUTS": "true"})
	_, span := einoTracer().Start(context.Background(), "chain")
	span.AddEvent("exception", trace.WithAttributes(
		attribute.String("exception.type", "*errors.errorString"),
		attribute.Int("count", 4242), attribute.Bool("flag", true), attribute.Float64("ratio", 0.75),
		attribute.IntSlice("counts", []int{7001, 7002}), attribute.StringSlice("words", []string{"alpha", "beta"})))
	span.End()
	events := one(t, app).Events()
	if len(events) != 1 {
		t.Fatalf("%d events", len(events))
	}
	for _, kv := range events[0].Attributes {
		if kv.Key == "exception.type" {
			if kv.Value.AsString() != "*errors.errorString" {
				t.Errorf("exception.type = %v", kv.Value.AsInterface())
			}
			continue
		}
		if kv.Value.Type() != attribute.STRING || kv.Value.AsString() != RedactedValue {
			t.Errorf("%s = %v, want the redaction marker", kv.Key, kv.Value.AsInterface())
		}
	}
}

const knownSecret = "known-secret-value-0001-abcdef"

// A credential the agent knows is replaced in the application's copy of a module span, as in the copy sent to Future AGI.
func TestKnownSecretsAreReplacedInTheApplicationCopy(t *testing.T) {
	app, fi := both(t, nil)
	RegisterSecret(knownSecret)
	linked := trace.SpanContext{}.WithTraceID(trace.TraceID{3}).WithSpanID(trace.SpanID{3}).WithTraceFlags(trace.FlagsSampled)
	ctx, span := einoTracer().Start(context.Background(), "chain "+knownSecret,
		trace.WithAttributes(attribute.String("input.value", "prompt "+knownSecret), attribute.StringSlice("tags", []string{"a", knownSecret})),
		trace.WithLinks(trace.Link{SpanContext: linked, Attributes: []attribute.KeyValue{attribute.String("link.note", knownSecret)}}))
	span.SetAttributes(attribute.String("tool.arguments", `{"token":"`+knownSecret+`"}`))
	trace.SpanFromContext(ctx).SetAttributes(attribute.String("late", knownSecret))
	span.AddEvent("event "+knownSecret, trace.WithAttributes(attribute.String("note", knownSecret)))
	span.RecordError(fmt.Errorf("failed with %s", knownSecret))
	span.SetName("renamed " + knownSecret)
	span.AddLink(trace.Link{SpanContext: linked, Attributes: []attribute.KeyValue{attribute.String("late.link", knownSecret)}})
	span.SetStatus(codes.Error, "status "+knownSecret)
	span.End()
	for name, rec := range map[string]*tracetest.SpanRecorder{"application copy": app, "Future AGI copy": fi} {
		got := dump(one(t, rec))
		if strings.Contains(got, knownSecret) {
			t.Errorf("%s holds the known secret: %s", name, got)
		}
		if !strings.Contains(got, "prompt ") {
			t.Errorf("%s lost the text around the secret: %s", name, got)
		}
	}
}

func BenchmarkModuleSpanWithKnownSecrets(b *testing.B) {
	m := moduleMask{cfg: Config{}}
	RegisterSecret(knownSecret)
	kv := attribute.String("input.value", strings.Repeat("a long prompt without a credential in it ", 40))
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		m.attribute(kv)
	}
}
