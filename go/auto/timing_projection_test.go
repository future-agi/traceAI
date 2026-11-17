package auto

import (
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
)

// The spec's protected TIMING family is reserved like usage. When even the
// protected set overflows the host attribute limit, timing is kept after usage
// and before endpoint provenance.
func TestTimingFamilyIsProtected(t *testing.T) {
	for _, key := range []string{"gen_ai.response.latency", "gen_ai.response.time_to_first_chunk", "gen_ai.response.time_to_first_text", "gen_ai.response.time_to_first_token", "gen_ai.server.time_to_first_token", "gen_ai.server.time_per_output_token", "gen_ai.server.request.duration"} {
		if protectedAttributePriority(attribute.Float64(key, 0.25)) < 0 {
			t.Errorf("%s is not in the protected set", key)
		}
	}
	values := []attribute.KeyValue{
		attribute.Int64("gen_ai.usage.total_tokens", 3),
		attribute.Float64("gen_ai.response.latency", 0.5),
		attribute.String("traceai.wire.endpoint", "openai.chat_completions"),
		attribute.String("server.address", "api.openai.com"),
	}
	var kept []string
	for _, kv := range budgetAttributes(values, 3) {
		kept = append(kept, string(kv.Key))
	}
	if got := strings.Join(kept, ","); got != "traceai.wire.attributes_truncated,gen_ai.usage.total_tokens,gen_ai.response.latency" {
		t.Fatalf("protected overflow kept %s", got)
	}
}
