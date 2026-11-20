package auto_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// limitedCopies is secretCopies on an application provider with an attribute value-length limit.
func limitedCopies(t *testing.T, limit int) (tracer trace.Tracer, flush func() (fi, app *spanSink)) {
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
	host := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp), sdktrace.WithRawSpanLimits(sdktrace.SpanLimits{
		AttributeValueLengthLimit: limit, AttributeCountLimit: 128, EventCountLimit: 128, LinkCountLimit: 128,
		AttributePerEventCountLimit: 128, AttributePerLinkCountLimit: 128}))
	t.Cleanup(func() { _ = host.Shutdown(context.Background()) })
	if _, err := auto.InitForTest(context.Background(), auto.WithTracerProvider(host), auto.WithCredentials("SURFACE_FI_KEY", "SURFACE_FI_SECRET")); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = auto.Shutdown(context.Background()) })
	return auto.Attach("eino").TracerProvider().Tracer("surfaces"), func() (*spanSink, *spanSink) {
		_ = auto.ForceFlush(context.Background())
		_ = host.ForceFlush(context.Background())
		return fi, app
	}
}

func linkContext() trace.SpanContext {
	return trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{9, 9}, SpanID: trace.SpanID{9, 9}, TraceFlags: trace.FlagsSampled})
}

// Two overlapping secrets, in both orders, with the longer one learned after the value was written: every surface of a
// module span (an event, an error message, a link at start and added later, the status) holds neither secret nor the
// longer secret's own fragment, on both copies, in the raw exported bytes.
func TestOverlappingSecretsOnEverySurfaceOnBothCopiesInBothOrders(t *testing.T) {
	const fragment = "PREFIX-FRAGMENT-"
	n := 0
	for _, shortFirst := range []bool{true, false} {
		for _, surface := range []string{"event attribute", "error message", "link at start", "late link", "status description"} {
			n++
			// Secrets live in a process-wide registry: each case needs its own.
			short := fmt.Sprintf("SURFACE-OVERLAP-%02d-ABCD", n)
			long := fragment + short
			name := "longer first, shorter later"
			if shortFirst {
				name = "shorter first, longer later"
			}
			t.Run(name+"/"+surface, func(t *testing.T) {
				tracer, flush := secretCopies(t)
				first, second := short, long
				if !shortFirst {
					first, second = long, short
				}
				auto.RegisterSecret(first)
				value := "see " + long + " here"
				kind := traceStart(attribute.String("gen_ai.span.kind", "CHAIN"))
				var span trace.Span
				switch surface {
				case "link at start":
					_, span = tracer.Start(context.Background(), "step", kind, trace.WithLinks(trace.Link{SpanContext: linkContext(), Attributes: []attribute.KeyValue{attribute.String("note", value)}}))
				default:
					_, span = tracer.Start(context.Background(), "step", kind)
				}
				switch surface {
				case "event attribute":
					span.AddEvent("noted", trace.WithAttributes(attribute.String("note", value)))
				case "error message":
					span.RecordError(errors.New("failed: " + value))
				case "late link":
					span.AddLink(trace.Link{SpanContext: linkContext(), Attributes: []attribute.KeyValue{attribute.String("note", value)}})
				case "status description":
					span.SetStatus(codes.Error, value)
				}
				auto.RegisterSecret(second) // learned after the value was written
				span.End()
				fi, app := flush()
				for copyName, sink := range map[string]*spanSink{"Future AGI": fi, "application": app} {
					spans, raw := sink.snapshot()
					if len(spans) != 1 {
						t.Fatalf("%s copy: %d spans, want 1", copyName, len(spans))
					}
					for _, leaked := range []string{short, long, fragment} {
						if bytes.Contains(raw, []byte(leaked)) {
							t.Errorf("%s copy contains %q", copyName, leaked)
						}
					}
				}
			})
		}
	}
}

// A secret that straddles the provider's attribute value-length limit leaves no fragment, and the copy sent to Future AGI
// respects the limit as the application's copy does: the secret is replaced on the whole value first, then the value is capped.
func TestSecretAtTheValueLimitBoundary(t *testing.T) {
	const limit = 64
	const secret = "STRADDLE-SECRET-ABCDEF"
	tracer, flush := limitedCopies(t, limit)
	auto.RegisterSecret(secret)
	value := strings.Repeat("a", limit-6) + secret + " tail"
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	span.SetAttributes(attribute.String("input.value", value))
	span.AddEvent("noted", trace.WithAttributes(attribute.String("note", value)))
	span.RecordError(errors.New(value))
	span.End()
	fi, app := flush()
	for name, sink := range map[string]*spanSink{"Future AGI": fi, "application": app} {
		spans, raw := sink.snapshot()
		if len(spans) != 1 {
			t.Fatalf("%s copy: %d spans, want 1", name, len(spans))
		}
		for _, fragment := range []string{"STRADD", "ADDLE-S", "-ABCDEF", "ECRET"} {
			if bytes.Contains(raw, []byte(fragment)) {
				t.Errorf("%s copy contains the fragment %q of a secret", name, fragment)
			}
		}
		if got := spans[0].attrs["input.value"]; utf8.RuneCountInString(got) > limit {
			t.Errorf("%s copy: input.value has %d runes, over the provider's limit of %d", name, utf8.RuneCountInString(got), limit)
		}
	}
}

func deepRecordError(span trace.Span, depth int) {
	if depth > 0 {
		deepRecordError(span, depth-1)
		return
	}
	span.RecordError(errors.New("deep"), trace.WithStackTrace(true))
}

// A recorded stack trace is a single capture of at most 2048 bytes, as the SDK records it, on both copies.
func TestRecordedStackTraceIsAtMost2048Bytes(t *testing.T) {
	_ = auto.Shutdown(context.Background())
	auto.ResetModulesForTest()
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	appRec, fiRec := tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
	host := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(appRec))
	t.Cleanup(func() { _ = host.Shutdown(context.Background()) })
	if _, err := auto.InitForTest(context.Background(), auto.WithTracerProvider(host), auto.WithSpanProcessor(fiRec)); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = auto.Shutdown(context.Background()) })
	_, span := auto.Attach("eino").TracerProvider().Tracer("deep").Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	deepRecordError(span, 60)
	span.End()
	for name, rec := range map[string]*tracetest.SpanRecorder{"application": appRec, "Future AGI": fiRec} {
		ended := rec.Ended()
		if len(ended) != 1 {
			t.Fatalf("%s copy: %d spans", name, len(ended))
		}
		found := false
		for _, e := range ended[0].Events() {
			for _, kv := range e.Attributes {
				if kv.Key == "exception.stacktrace" {
					found = true
					if n := len(kv.Value.AsString()); n > 2048 {
						t.Errorf("%s copy: stack trace of %d bytes, want at most 2048", name, n)
					}
				}
			}
		}
		if !found {
			t.Errorf("%s copy: no stack trace", name)
		}
	}
}
