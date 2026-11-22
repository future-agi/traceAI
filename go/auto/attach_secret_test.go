package auto_test

import (
	"bytes"
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// secretCopies builds both destinations of an Eino-style module span: Future AGI (queued, exported at flush) and the
// application's own provider, whose exporter takes each span as it ends. It returns what each received.
func secretCopies(t *testing.T) (tracer trace.Tracer, flush func() (fi, app *spanSink)) {
	t.Helper()
	_ = auto.Shutdown(context.Background())
	auto.ResetModulesForTest()
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	fi, app := newSpanSink(t), newSpanSink(t)
	t.Setenv("FI_BASE_URL", fi.URL)
	exp, err := otlptracehttp.New(context.Background(), otlptracehttp.WithEndpointURL(app.URL+"/v1/traces"), otlptracehttp.WithInsecure())
	if err != nil {
		t.Fatal(err)
	}
	host := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	t.Cleanup(func() { _ = host.Shutdown(context.Background()) })
	if _, err := auto.InitForTest(context.Background(), auto.WithTracerProvider(host), auto.WithCredentials("SECRET_COPY_FI_KEY", "SECRET_COPY_FI_SECRET")); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = auto.Shutdown(context.Background()) })
	tracer = auto.Attach("eino").TracerProvider().Tracer("secrets")
	return tracer, func() (*spanSink, *spanSink) {
		_ = auto.ForceFlush(context.Background())
		_ = host.ForceFlush(context.Background())
		return fi, app
	}
}

// Two overlapping secrets, learned in either order, while a value that holds the longer one is already written: at
// export neither copy contains either secret, and the longer match is replaced as a whole.
func TestOverlappingSecretsAreReplacedOnBothCopiesInBothOrders(t *testing.T) {
	for _, tc := range []struct {
		name               string
		first, second      string // learned first and second
		short, long, value string
	}{
		{"shorter first, longer later", "short", "long", "OVERLAP-SECRET-1234", "PREFIX-OVERLAP-SECRET-1234", "see PREFIX-OVERLAP-SECRET-1234 here"},
		{"longer first, shorter later", "long", "short", "OVERLAP-SECRET-5678", "PREFIX-OVERLAP-SECRET-5678", "see PREFIX-OVERLAP-SECRET-5678 here"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tracer, flush := secretCopies(t)
			learn := map[string]string{"short": tc.short, "long": tc.long}
			auto.RegisterSecret(learn[tc.first])
			_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
			span.SetAttributes(attribute.String("input.value", tc.value))
			auto.RegisterSecret(learn[tc.second]) // learned after the value was written
			span.End()
			fi, app := flush()
			want := "see " + wire.CredentialRedacted + " here"
			for name, sink := range map[string]*spanSink{"Future AGI": fi, "application": app} {
				spans, raw := sink.snapshot()
				if len(spans) != 1 {
					t.Fatalf("%s copy: %d spans, want 1", name, len(spans))
				}
				if bytes.Contains(raw, []byte(tc.short)) || bytes.Contains(raw, []byte(tc.long)) {
					t.Errorf("%s copy contains a secret", name)
				}
				if got := spans[0].attrs["input.value"]; got != want {
					t.Errorf("%s copy input.value = %q, want %q", name, got, want)
				}
			}
		})
	}
}

type recordedError struct{}

func (recordedError) Error() string { return "recorded failure" }

// RecordError on a module span writes the exception event the SDK writes: the same exception.type, and the stack
// trace when the caller asks for one.
func TestRecordErrorMatchesTheSDK(t *testing.T) {
	rec := tracetest.NewSpanRecorder()
	_ = auto.Shutdown(context.Background())
	auto.ResetModulesForTest()
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	host := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	t.Cleanup(func() { _ = host.Shutdown(context.Background()) })
	if _, err := auto.InitForTest(context.Background(), auto.WithTracerProvider(host), auto.WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = auto.Shutdown(context.Background()) })
	attr := func(span sdktrace.ReadOnlySpan, key string) (string, bool) {
		for _, e := range span.Events() {
			for _, kv := range e.Attributes {
				if string(kv.Key) == key {
					return kv.Value.AsString(), true
				}
			}
		}
		return "", false
	}
	for _, err := range []error{recordedError{}, errors.New("plain")} {
		_, ref := host.Tracer("sdk").Start(context.Background(), "reference")
		ref.RecordError(err, trace.WithStackTrace(true))
		ref.End()
		_, span := auto.Attach("eino").TracerProvider().Tracer("m").Start(context.Background(), "module", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
		span.RecordError(err, trace.WithStackTrace(true))
		span.End()
		ended := rec.Ended()
		sdkSpan, ours := ended[len(ended)-2], ended[len(ended)-1]
		wantType, _ := attr(sdkSpan, "exception.type")
		if gotType, _ := attr(ours, "exception.type"); gotType != wantType {
			t.Errorf("exception.type = %q, want the SDK's %q", gotType, wantType)
		}
		if st, ok := attr(ours, "exception.stacktrace"); !ok || !strings.Contains(st, "goroutine") {
			t.Errorf("no stack trace on %T although WithStackTrace(true) was given", err)
		}
	}
}
