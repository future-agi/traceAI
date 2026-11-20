package auto

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// benchMask is a mask with the default configuration and several registered secrets, as a long-running
// process has after a few model calls.
func benchMask(b *testing.B, secrets int) moduleMask {
	b.Helper()
	for i := 0; i < secrets; i++ {
		RegisterSecret(fmt.Sprintf("bench-registered-secret-%02d-abcdef", i))
	}
	return moduleMask{cfg: Config{}}
}

var benchText = strings.Repeat("a typical prompt or model reply of ordinary words ", 20)

// The baseline: structural capture and the privacy switches, no secret replacement.
func BenchmarkModuleMaskCaptured(b *testing.B) {
	m := benchMask(b, 8)
	kvs := []attribute.KeyValue{attribute.String("input.value", benchText), attribute.String("output.value", benchText)}
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		_ = m.captured(kvs)
	}
}

// Secret replacement with several secrets registered and none in the text.
func BenchmarkModuleMaskReplacedNoHit(b *testing.B) {
	m := benchMask(b, 8)
	kvs := []attribute.KeyValue{attribute.String("input.value", benchText), attribute.String("output.value", benchText)}
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		_ = m.replaced(kvs)
	}
}

// Secret replacement when each value holds a registered secret.
func BenchmarkModuleMaskReplacedHit(b *testing.B) {
	m := benchMask(b, 8)
	hit := benchText + " bench-registered-secret-03-abcdef " + benchText
	kvs := []attribute.KeyValue{attribute.String("input.value", hit), attribute.String("output.value", hit)}
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		_ = m.replaced(kvs)
	}
}

// A whole module span, start to end, as the Eino handler writes one.
func BenchmarkModuleSpanStartToEnd(b *testing.B) {
	_ = Shutdown(context.Background())
	resetModulesForTest()
	b.Setenv("FI_CONFIG_FILE", b.TempDir()+"/absent.toml")
	if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
		b.Fatal(err)
	}
	b.Cleanup(func() { _ = Shutdown(context.Background()) })
	benchMask(b, 8)
	tracer := Attach("eino").TracerProvider().Tracer("bench")
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_, span := tracer.Start(context.Background(), "step", trace.WithAttributes(attribute.String("gen_ai.span.kind", "CHAIN")))
		span.SetAttributes(attribute.String("input.value", benchText), attribute.String("output.value", benchText))
		span.End()
	}
}
