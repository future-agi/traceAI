package auto_test

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func integrationStatus(t *testing.T, name string) string {
	t.Helper()
	for _, i := range auto.Report().Integrations {
		if i.Name == name {
			return i.Status
		}
	}
	t.Fatalf("no %q integration in the report", name)
	return ""
}

func attachSetup(t *testing.T) *tracetest.SpanRecorder {
	t.Helper()
	_ = auto.Shutdown(context.Background())
	auto.ResetModulesForTest()
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	rec := tracetest.NewSpanRecorder()
	if _, err := auto.InitForTest(context.Background(), auto.WithSpanProcessor(rec)); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = auto.Shutdown(context.Background()) })
	return rec
}

// A dedicated module that attaches to the agent reports as "auto"; every other
// dedicated module stays "manual".
func TestAttachMarksTheIntegrationAuto(t *testing.T) {
	attachSetup(t)
	if got := integrationStatus(t, "eino"); got != "manual" {
		t.Fatalf("eino before Attach = %q, want manual", got)
	}
	auto.Attach("eino")
	if got := integrationStatus(t, "eino"); got != "auto" {
		t.Fatalf("eino after Attach = %q, want auto", got)
	}
	if got := integrationStatus(t, "genai"); got != "manual" {
		t.Fatalf("genai = %q, want manual", got)
	}
}

// Spans started from the attached provider travel the agent's route.
func TestAttachedProviderRoutesSpansThroughTheAgent(t *testing.T) {
	rec := attachSetup(t)
	tracer := auto.Attach("eino").TracerProvider().Tracer("test")
	_, span := tracer.Start(context.Background(), "chain", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	span.End()
	if got := len(rec.Ended()); got != 1 {
		t.Fatalf("agent route received %d spans, want 1", got)
	}
}

// FI_INSTRUMENTATIONS is an allowlist: without the module's name the attached
// provider records nothing and the report says disabled.
func TestAttachHonoursTheInstrumentationAllowlist(t *testing.T) {
	t.Setenv("FI_INSTRUMENTATIONS", "wire")
	rec := attachSetup(t)
	a := auto.Attach("eino")
	_, span := a.TracerProvider().Tracer("test").Start(context.Background(), "chain")
	span.End()
	if got := len(rec.Ended()); got != 0 {
		t.Fatalf("disallowed module recorded %d spans", got)
	}
	if got := integrationStatus(t, "eino"); got != "disabled" {
		t.Fatalf("eino = %q, want disabled", got)
	}
}

// The module reads privacy switches from the agent's resolved configuration.
func TestAttachTraceConfigIsTheAgents(t *testing.T) {
	t.Setenv("FI_HIDE_INPUTS", "true")
	attachSetup(t)
	if !auto.Attach("eino").TraceConfig().HideInputs {
		t.Fatal("FI_HIDE_INPUTS did not reach the attached module")
	}
}

func traceStart(kvs ...attribute.KeyValue) trace.SpanStartOption { return trace.WithAttributes(kvs...) }

// The copy of a module span that goes to the application's own provider obeys
// every FI_HIDE_* switch exactly as the agent's wire spans do.
func TestAttachedSpansAreMaskedOnTheApplicationProvider(t *testing.T) {
	cases := map[string]struct{ env, key, value string }{
		"inputs":          {"FI_HIDE_INPUTS", "input.value", "the question"},
		"input messages":  {"FI_HIDE_INPUT_MESSAGES", "input.value", "the question"},
		"input text":      {"FI_HIDE_INPUT_TEXT", "input.value", "the question"},
		"outputs":         {"FI_HIDE_OUTPUTS", "output.value", "the answer"},
		"output messages": {"FI_HIDE_OUTPUT_MESSAGES", "output.value", "the answer"},
		"output text":     {"FI_HIDE_OUTPUT_TEXT", "output.value", "the answer"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			_ = auto.Shutdown(context.Background())
			auto.ResetModulesForTest()
			t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
			t.Setenv(c.env, "true")
			host := tracetest.NewSpanRecorder()
			app := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(host))
			t.Cleanup(func() { _ = app.Shutdown(context.Background()) })
			if _, err := auto.InitForTest(context.Background(), auto.WithTracerProvider(app), auto.WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = auto.Shutdown(context.Background()) })
			tracer := auto.Attach("eino").TracerProvider().Tracer("test")
			_, span := tracer.Start(context.Background(), "chain", traceStart(attribute.String("gen_ai.span.kind", "CHAIN"), attribute.String(c.key, c.value)))
			span.SetAttributes(attribute.String(c.key, c.value))
			span.End()
			ended := host.Ended()
			if len(ended) != 1 {
				t.Fatalf("application provider received %d spans, want 1", len(ended))
			}
			for _, kv := range ended[0].Attributes() {
				if string(kv.Key) == c.key && kv.Value.AsString() != "__REDACTED__" {
					t.Fatalf("%s=true left %s as %q on the application's copy", c.env, c.key, kv.Value.AsString())
				}
			}
		})
	}
}

// A hook that ignores its deadline must not stop queued spans from being
// exported, and Shutdown must return within its budget.
func TestShutdownHookThatBlocksStillFlushes(t *testing.T) {
	_ = auto.Shutdown(context.Background())
	auto.ResetModulesForTest()
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	sink := newSpanSink(t)
	t.Setenv("FI_BASE_URL", sink.URL)
	if _, err := auto.InitForTest(context.Background(), auto.WithCredentials("HOOK_FI_KEY", "HOOK_FI_SECRET")); err != nil {
		t.Fatal(err)
	}
	a := auto.Attach("eino")
	block := make(chan struct{})
	defer close(block)
	a.OnShutdown(func(context.Context) error { <-block; return nil })
	_, span := a.TracerProvider().Tracer("test").Start(context.Background(), "queued", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	span.End()
	start := time.Now()
	done := make(chan struct{})
	go func() { _ = auto.Shutdown(context.Background()); close(done) }()
	select {
	case <-done:
	case <-time.After(8 * time.Second):
		t.Fatal("Shutdown is still blocked by a hook that ignores its deadline")
	}
	if took := time.Since(start); took > 4500*time.Millisecond {
		t.Fatalf("Shutdown took %v with a blocked hook, want within its 5s budget", took)
	}
	spans, _ := sink.snapshot()
	if len(spans) != 1 {
		t.Fatalf("collector received %d queued spans, want 1", len(spans))
	}
}

// A concurrent Shutdown honours its own context while another caller's hooks
// are still running.
func TestConcurrentShutdownIsNotHeldByTheHooksOfAnother(t *testing.T) {
	_ = auto.Shutdown(context.Background())
	auto.ResetModulesForTest()
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	if _, err := auto.InitForTest(context.Background(), auto.WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
		t.Fatal(err)
	}
	block := make(chan struct{})
	defer close(block)
	auto.Attach("eino").OnShutdown(func(context.Context) error { <-block; return nil })
	first := make(chan struct{})
	go func() { _ = auto.Shutdown(context.Background()); close(first) }()
	time.Sleep(200 * time.Millisecond)
	ctx, cancel := context.WithTimeout(context.Background(), 400*time.Millisecond)
	defer cancel()
	start := time.Now()
	_ = auto.Shutdown(ctx)
	if took := time.Since(start); took > 1500*time.Millisecond {
		t.Fatalf("a Shutdown with a 400ms deadline took %v behind another caller's hooks", took)
	}
	<-first
}
