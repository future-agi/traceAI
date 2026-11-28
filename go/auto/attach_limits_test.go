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
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
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

// What the SDK keeps of an event's and a link's attributes (its per-event and per-link limits, the first ones in order, duplicate keys included)
// is what the copy for Future AGI carries, with the value each was written with.
func TestEventAttributesFollowTheSDKLimitsOnBothCopies(t *testing.T) {
	tracer, flush := limitedCopiesWith(t, sdktrace.SpanLimits{AttributeValueLengthLimit: 4096, AttributeCountLimit: 128, EventCountLimit: 128, LinkCountLimit: 128,
		AttributePerEventCountLimit: 3, AttributePerLinkCountLimit: 2, AttributeValueDepthLimit: -1})
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	span.AddEvent("noted", trace.WithTimestamp(time.Unix(1_700_000_000, 0)), trace.WithAttributes(attribute.String("k", "stale-duplicate-value"), attribute.String("k", "new"),
		attribute.String("a", "1"), attribute.String("b", "2"), attribute.String("c-over-the-limit", "3")))
	span.AddLink(spanIDLink(1, attribute.String("l1", "x"), attribute.String("l2", "y"), attribute.String("l3-over-the-limit", "z")))
	span.End()
	fi, app := flush()
	appSpan, fiSpan := onlyModuleSpan(t, "application", app), onlyModuleSpan(t, "Future AGI", fi)
	if len(appSpan.events) != 1 || appSpan.events[0].attrs["k"] != "new" {
		t.Fatalf("application copy event %+v", appSpan.events)
	}
	if !reflect.DeepEqual(fiSpan.events, appSpan.events) || !reflect.DeepEqual(fiSpan.links, appSpan.links) {
		t.Fatalf("Future AGI events %+v links %+v, application events %+v links %+v", fiSpan.events, fiSpan.links, appSpan.events, appSpan.links)
	}
	for name, sink := range map[string]*spanSink{"Future AGI": fi, "application": app} {
		if _, raw := sink.snapshot(); bytes.Contains(raw, []byte("over-the-limit")) {
			t.Errorf("%s copy carries an attribute the SDK dropped", name)
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

// The record is bounded by the provider's limits, and the SDK's dropped counts stay what they would have been.
func TestManyEventsAndLinksKeepTheNewestAndTheDroppedCounts(t *testing.T) {
	tracer, flush := limitedCopies(t, 4096)
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	for i := 0; i < 1000; i++ {
		span.AddEvent(fmt.Sprintf("event-%04d", i), trace.WithTimestamp(time.Unix(1_700_000_000+int64(i), 0)))
		span.AddLink(spanIDLink(i+1, attribute.String("n", fmt.Sprintf("content-%04d", i))))
	}
	span.End()
	fi, app := flush()
	appSpan, fiSpan := onlyModuleSpan(t, "application", app), onlyModuleSpan(t, "Future AGI", fi)
	if len(appSpan.events) != 128 || appSpan.events[0].name != "event-0872" || appSpan.droppedEvents != 872 || appSpan.droppedLinks != 872 {
		t.Fatalf("application copy: %d events from %q, dropped %d events and %d links; want 128 from event-0872, 872 and 872",
			len(appSpan.events), appSpan.events[0].name, appSpan.droppedEvents, appSpan.droppedLinks)
	}
	if !reflect.DeepEqual(fiSpan.events, appSpan.events) || !reflect.DeepEqual(fiSpan.links, appSpan.links) {
		t.Fatalf("Future AGI copy differs from the application's")
	}
	if fiSpan.droppedEvents != 872 || fiSpan.droppedLinks != 872 {
		t.Fatalf("Future AGI copy reports %d events and %d links dropped, want 872 each", fiSpan.droppedEvents, fiSpan.droppedLinks)
	}
}

// The record held while a span is open never exceeds the provider's limits.
func TestRecordNeverExceedsTheProviderLimits(t *testing.T) {
	tracer, flush := limitedCopies(t, 4096)
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	for i := 0; i < 600; i++ {
		span.AddEvent(fmt.Sprintf("event-%04d", i))
		span.AddLink(spanIDLink(i+1, attribute.String("n", "x")))
		if events, links := auto.RecordSizesForTest(span); events > 128 || links > 128 || events < 0 || links < 0 {
			t.Fatalf("after %d adds the record holds %d events and %d links, the limit is 128", i+1, events, links)
		}
	}
	if events, links := auto.RecordSizesForTest(span); events != 128 || links != 128 {
		t.Fatalf("record holds %d events and %d links, want 128 each", events, links)
	}
	span.End()
	flush()
}

// AddEvent and AddLink from another goroutine while the span ends cannot shift what the Future AGI copy holds against the
// application's copy: what End replayed is what the record keeps.
func TestConcurrentAddsDuringEndKeepBothCopiesAligned(t *testing.T) {
	tracer, flush := limitedCopies(t, 4096)
	const spans = 40
	for n := 0; n < spans; n++ {
		_, span := tracer.Start(context.Background(), fmt.Sprintf("step-%02d", n), traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
		for i := 0; i < 128; i++ {
			span.AddEvent(fmt.Sprintf("seed-%03d", i))
			span.AddLink(spanIDLink(i+1, attribute.String("n", "seed")))
		}
		stop, done := make(chan struct{}), make(chan struct{})
		go func() {
			defer close(done)
			for i := 0; ; i++ {
				select {
				case <-stop:
					return
				default:
				}
				span.AddEvent(fmt.Sprintf("late-%06d", i))
				span.AddLink(spanIDLink(1000+i, attribute.String("n", "late")))
			}
		}()
		time.Sleep(time.Duration(n%5) * 50 * time.Microsecond)
		span.End()
		close(stop)
		<-done
	}
	fi, app := flush()
	appSpans, _ := app.snapshot()
	fiSpans, _ := fi.snapshot()
	if len(appSpans) != spans || len(fiSpans) != spans {
		t.Fatalf("spans: application %d, Future AGI %d, want %d", len(appSpans), len(fiSpans), spans)
	}
	byName := map[string]exportedSpan{}
	for _, s := range fiSpans {
		byName[s.name] = s
	}
	for _, a := range appSpans {
		f, ok := byName[a.name]
		if !ok {
			t.Fatalf("Future AGI copy lacks %s", a.name)
		}
		if !reflect.DeepEqual(f.events, a.events) || !reflect.DeepEqual(f.links, a.links) {
			t.Fatalf("%s: events/links of the Future AGI copy differ from the application's (events %d vs %d, links %d vs %d)", a.name, len(f.events), len(a.events), len(f.links), len(a.links))
		}
	}
}

// The SDK's refusal test sees the attributes after masking: a link with an invalid context whose attributes are all hidden is
// refused, on both copies, and leaves no placeholder in either.
func TestLinkRefusedAfterMaskingLeavesNothing(t *testing.T) {
	t.Setenv("FI_HIDE_INPUTS", "true")
	tracer, flush := limitedCopies(t, 4096)
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")),
		trace.WithLinks(trace.Link{Attributes: []attribute.KeyValue{attribute.String("gen_ai.input.messages", "hidden")}}))
	for i := 0; i < 300; i++ {
		span.AddLink(trace.Link{Attributes: []attribute.KeyValue{attribute.String("gen_ai.input.messages", "hidden")}})
	}
	if _, links := auto.RecordSizesForTest(span); links != 0 {
		t.Fatalf("record holds %d links of a refused kind, want 0", links)
	}
	span.AddLink(spanIDLink(1, attribute.String("n", "kept")))
	span.End()
	fi, app := flush()
	appSpan, fiSpan := onlyModuleSpan(t, "application", app), onlyModuleSpan(t, "Future AGI", fi)
	if len(appSpan.links) != 1 || appSpan.droppedLinks != 0 || len(fiSpan.links) != 1 || fiSpan.droppedLinks != 0 {
		t.Fatalf("application: %d links, %d dropped; Future AGI: %d links, %d dropped; want 1 and 0 on both", len(appSpan.links), appSpan.droppedLinks, len(fiSpan.links), fiSpan.droppedLinks)
	}
}

// The SDK keeps the FIRST attributes of an event or a link up to its limit, in order, duplicate keys included: the copy for
// Future AGI carries the value of each one it kept, and nothing the SDK never stored.
func TestAttributesPastThePerEventLimitFollowTheSDKFirstN(t *testing.T) {
	tracer, flush := limitedCopiesWith(t, sdktrace.SpanLimits{AttributeValueLengthLimit: 4096, AttributeCountLimit: 128, EventCountLimit: 128, LinkCountLimit: 128,
		AttributePerEventCountLimit: 2, AttributePerLinkCountLimit: 2, AttributeValueDepthLimit: -1})
	_, span := tracer.Start(context.Background(), "step", traceStart(attribute.String("gen_ai.span.kind", "CHAIN")))
	span.AddEvent("noted", trace.WithTimestamp(time.Unix(1_700_000_000, 0)), trace.WithAttributes(attribute.String("k", "first"), attribute.String("a", "second"), attribute.String("k", "third-past-the-limit")))
	span.AddLink(spanIDLink(1, attribute.String("k", "first"), attribute.String("a", "second"), attribute.String("k", "third-past-the-limit")))
	span.End()
	fi, app := flush()
	appSpan, fiSpan := onlyModuleSpan(t, "application", app), onlyModuleSpan(t, "Future AGI", fi)
	if appSpan.events[0].attrs["k"] != "first" || appSpan.links[0].attrs["k"] != "first" {
		t.Fatalf("application copy: %+v %+v", appSpan.events, appSpan.links)
	}
	if !reflect.DeepEqual(fiSpan.events, appSpan.events) || !reflect.DeepEqual(fiSpan.links, appSpan.links) {
		t.Fatalf("Future AGI events %+v links %+v, application events %+v links %+v", fiSpan.events, fiSpan.links, appSpan.events, appSpan.links)
	}
	if _, raw := fi.snapshot(); bytes.Contains(raw, []byte("past-the-limit")) {
		t.Error("Future AGI copy carries a value the SDK never stored")
	}
}
