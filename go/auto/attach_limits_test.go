package auto_test

import (
	"bytes"
	"context"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

func spanIDLink(n int, attrs ...attribute.KeyValue) trace.Link {
	return trace.Link{
		SpanContext: trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{9, 9}, SpanID: trace.SpanID{byte(n >> 8), byte(n), 7}, TraceFlags: trace.FlagsSampled}),
		Attributes:  attrs,
	}
}

func onlyModuleSpan(t *testing.T, name string, sink *spanSink) exportedSpan {
	t.Helper()
	spans, _ := sink.snapshot()
	if len(spans) != 1 {
		t.Fatalf("%s copy: %d spans, want 1", name, len(spans))
	}
	return spans[0]
}

// Past the SDK's event limit the SDK drops the oldest events. The copy for Future AGI keeps exactly the events the
// application's copy keeps, with the content and the timestamp each was written with.
func TestEventsPastTheSDKLimitAreAlignedOnBothCopies(t *testing.T) {
	tracer, flush := limitedCopies(t, 4096)
	start := time.Unix(1_700_000_000, 0)
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	for i := 0; i < 130; i++ {
		span.AddEvent(fmt.Sprintf("event-%03d", i), trace.WithTimestamp(start.Add(time.Duration(i)*time.Second)), trace.WithAttributes(attribute.String("n", fmt.Sprintf("content-%03d", i))))
	}
	span.End()
	fi, app := flush()
	appSpan, fiSpan := onlyModuleSpan(t, "application", app), onlyModuleSpan(t, "Future AGI", fi)
	if len(appSpan.events) != 128 {
		t.Fatalf("application copy keeps %d events, want 128", len(appSpan.events))
	}
	if appSpan.events[0].name != "event-002" || appSpan.events[127].name != "event-129" {
		t.Fatalf("application copy keeps %q..%q, want event-002..event-129", appSpan.events[0].name, appSpan.events[127].name)
	}
	if !reflect.DeepEqual(fiSpan.events, appSpan.events) {
		for i := range fiSpan.events {
			if i < len(appSpan.events) && !reflect.DeepEqual(fiSpan.events[i], appSpan.events[i]) {
				t.Fatalf("Future AGI event %d is %+v, the application's is %+v", i, fiSpan.events[i], appSpan.events[i])
			}
		}
		t.Fatalf("Future AGI copy keeps %d events, the application's copy %d", len(fiSpan.events), len(appSpan.events))
	}
}

// The same for links, and a link the SDK refuses (invalid, no attributes) does not shift the others.
func TestLinksPastTheSDKLimitAreAlignedOnBothCopies(t *testing.T) {
	tracer, flush := limitedCopies(t, 4096)
	links := []trace.Link{{}} // refused by the SDK
	for i := 0; i < 130; i++ {
		links = append(links, spanIDLink(i+1, attribute.String("n", fmt.Sprintf("content-%03d", i))))
	}
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")), trace.WithLinks(links...))
	span.End()
	fi, app := flush()
	appSpan, fiSpan := onlyModuleSpan(t, "application", app), onlyModuleSpan(t, "Future AGI", fi)
	if len(appSpan.links) != 128 || appSpan.links[0].attrs["n"] != "content-002" {
		t.Fatalf("application copy keeps %d links from %q, want 128 from content-002", len(appSpan.links), appSpan.links[0].attrs["n"])
	}
	if !reflect.DeepEqual(fiSpan.links, appSpan.links) {
		t.Fatalf("Future AGI links differ from the application's: %d vs %d, first %v vs %v", len(fiSpan.links), len(appSpan.links), fiSpan.links[0].attrs, appSpan.links[0].attrs)
	}
}

func TestARefusedLinkDoesNotShiftTheOthers(t *testing.T) {
	tracer, flush := limitedCopies(t, 4096)
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")),
		trace.WithLinks(trace.Link{}, spanIDLink(1, attribute.String("n", "first")), spanIDLink(2, attribute.String("n", "second"))))
	span.End()
	fi, app := flush()
	appSpan, fiSpan := onlyModuleSpan(t, "application", app), onlyModuleSpan(t, "Future AGI", fi)
	if len(appSpan.links) != 2 || !reflect.DeepEqual(fiSpan.links, appSpan.links) {
		t.Fatalf("links differ: Future AGI %+v, application %+v", fiSpan.links, appSpan.links)
	}
	if fiSpan.links[0].attrs["n"] != "first" || fiSpan.links[1].attrs["n"] != "second" {
		t.Fatalf("Future AGI links carry %+v", fiSpan.links)
	}
}

// Duplicate keys inside one event keep the last value, as the SDK keeps it, on both copies.
func TestDuplicateEventAttributesMatchOnBothCopies(t *testing.T) {
	tracer, flush := limitedCopies(t, 4096)
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	span.AddEvent("noted", trace.WithTimestamp(time.Unix(1_700_000_000, 0)), trace.WithAttributes(attribute.String("k", "old"), attribute.String("k", "new")))
	span.End()
	fi, app := flush()
	appSpan, fiSpan := onlyModuleSpan(t, "application", app), onlyModuleSpan(t, "Future AGI", fi)
	if !reflect.DeepEqual(fiSpan.events, appSpan.events) || appSpan.events[0].attrs["k"] != "new" {
		t.Fatalf("Future AGI %+v, application %+v", fiSpan.events, appSpan.events)
	}
	for name, sink := range map[string]*spanSink{"Future AGI": fi, "application": app} {
		if _, raw := sink.snapshot(); bytes.Contains(raw, []byte("old")) {
			t.Errorf("%s copy carries the overwritten duplicate value", name)
		}
	}
}

// A secret learned after a value was written, which straddles the provider's value-length limit, leaves no fragment on
// either copy: the value is replaced whole, then capped.
func TestSecretLearnedAfterTheWriteAtTheValueLimitBoundary(t *testing.T) {
	const limit = 64
	const secret = "LATE-STRADDLE-SECRET-ABCDEF"
	tracer, flush := limitedCopies(t, limit)
	value := strings.Repeat("a", limit-6) + secret + " tail"
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	span.SetAttributes(attribute.String("input.value", value))
	auto.RegisterSecret(secret) // learned after the write
	span.End()
	fi, app := flush()
	for name, sink := range map[string]*spanSink{"Future AGI": fi, "application": app} {
		spans, raw := sink.snapshot()
		if len(spans) != 1 {
			t.Fatalf("%s copy: %d spans, want 1", name, len(spans))
		}
		for _, fragment := range []string{"LATE-ST", "STRADDLE", "-ABCDEF", "ECRET", "LATE"} {
			if bytes.Contains(raw, []byte(fragment)) {
				t.Errorf("%s copy contains the fragment %q of a secret", name, fragment)
			}
		}
	}
}
