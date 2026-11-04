package traceaiopenai

import (
	"context"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func attrMap(s sdktrace.ReadOnlySpan) map[string]string {
	m := map[string]string{}
	for _, kv := range s.Attributes() {
		m[string(kv.Key)] = kv.Value.Emit()
	}
	return m
}

// AC-13 and V-D: ContextAttributes reads the ids from the context, reaches
// contexts derived from it, and never invents one for a bare context.
func TestContextAttributesComeFromContextOnly(t *testing.T) {
	rec := tracetest.NewSpanRecorder()
	tr := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec)).Tracer("t")

	ctx := ContextWithUserID(ContextWithSessionID(context.Background(), "sess-1"), "user-7")
	ctx, parent := tr.Start(ctx, "parent", trace.WithAttributes(ContextAttributes(ctx)...))
	_, child := tr.Start(ctx, "child", trace.WithAttributes(ContextAttributes(ctx)...))
	child.End()
	parent.End()

	bareCtx := context.Background()
	_, bare := tr.Start(bareCtx, "bare", trace.WithAttributes(ContextAttributes(bareCtx)...))
	bare.End()

	byName := map[string]map[string]string{}
	for _, s := range rec.Ended() {
		byName[s.Name()] = attrMap(s)
	}
	for _, name := range []string{"parent", "child"} {
		if byName[name][SessionID] != "sess-1" || byName[name][UserID] != "user-7" {
			t.Errorf("%s attrs = %v", name, byName[name])
		}
	}
	if len(byName["bare"]) != 0 {
		t.Errorf("a bare context must not get ids: %v", byName["bare"])
	}
}

func TestEmptyIDsAreIgnored(t *testing.T) {
	ctx := ContextWithUserID(ContextWithSessionID(context.Background(), ""), "")
	if kvs := ContextAttributes(ctx); len(kvs) != 0 {
		t.Fatalf("empty ids produced %v", kvs)
	}
	if _, ok := SessionIDFromContext(nil); ok { //nolint:staticcheck // nil context is handled on purpose
		t.Fatal("nil context")
	}
}
