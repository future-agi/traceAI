package auto

import (
	"bytes"
	"log"
	"reflect"
	"strings"
	"sync"
	"testing"
)

// An OpenTelemetry SDK whose span limits the agent cannot read gets its own
// one-time warning that names the SDK, not the generic warning that points at
// credentials, configuration and the collector. Both limit readers go through
// spanLimit, so the test hands it layouts that lack the limits.
func TestSpanLimitsWarningNamesTheSDK(t *testing.T) {
	var logs bytes.Buffer
	old := log.Writer()
	log.SetOutput(&logs)
	t.Cleanup(func() { log.SetOutput(old) })
	// A fresh once, so the warning is printed again on every run (-count=2).
	spanLimitsOnce = sync.Once{}
	for _, layout := range []any{
		struct{ other int }{},
		struct{ spanLimits int }{},
		struct{ spanLimits struct{ Other int } }{},
		struct {
			spanLimits struct{ AttributeCountLimit, AttributeValueLengthLimit string }
		}{},
	} {
		for _, field := range []string{"AttributeCountLimit", "AttributeValueLengthLimit"} {
			if got := spanLimit(reflect.ValueOf(layout), field); got != 0 {
				t.Fatalf("%T: an unreadable layout must fail closed to 0, got %d", layout, got)
			}
		}
	}
	if got := strings.Count(logs.String(), spanLimitsWarning); got != 1 {
		t.Fatalf("want the span-limits warning exactly once, got %d: %q", got, logs.String())
	}
	if strings.Contains(logs.String(), "instrumentation unavailable") {
		t.Fatalf("an unreadable layout printed the generic warning: %q", logs.String())
	}
	if !strings.Contains(spanLimitsWarning, "OpenTelemetry SDK") || strings.Contains(spanLimitsWarning, "credentials") {
		t.Fatal("the warning must name the OpenTelemetry SDK as its cause")
	}
	// A layout that has the field returns its value.
	if got := spanLimit(reflect.ValueOf(struct {
		spanLimits struct{ AttributeCountLimit int }
	}{spanLimits: struct{ AttributeCountLimit int }{AttributeCountLimit: 7}}), "AttributeCountLimit"); got != 7 {
		t.Fatalf("a readable layout must return its limit, got %d", got)
	}
}
