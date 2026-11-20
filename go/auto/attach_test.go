package auto_test

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel/attribute"
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
