package auto

import (
	"bytes"
	"log"
	"strings"
	"testing"
)

// An OpenTelemetry SDK whose span limits the agent cannot read gets its own
// one-time warning that names the SDK, not the generic warning that points at
// credentials, configuration and the collector.
func TestSpanLimitsWarningNamesTheSDK(t *testing.T) {
	var logs bytes.Buffer
	old := log.Writer()
	log.SetOutput(&logs)
	t.Cleanup(func() { log.SetOutput(old) })
	spanLimitsUnreadable()
	spanLimitsUnreadable()
	if got := strings.Count(logs.String(), spanLimitsWarning); got != 1 {
		t.Fatalf("want the span-limits warning exactly once, got %d: %q", got, logs.String())
	}
	if !strings.Contains(spanLimitsWarning, "OpenTelemetry SDK") || strings.Contains(spanLimitsWarning, "credentials") {
		t.Fatal("the warning must name the OpenTelemetry SDK as its cause")
	}
}
