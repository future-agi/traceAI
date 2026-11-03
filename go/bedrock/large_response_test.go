package traceaibedrock

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// Large output is a supported Messages response, not an unknown schema.
// A capture budget may truncate text, but must not erase service usage.
func TestLargeInvokeResponseUsage(t *testing.T) {
	for _, hide := range []bool{false, true} {
		name := "capturing"
		if hide {
			name = "hidden"
		}
		t.Run(name, func(t *testing.T) {
			p, e := provider(t)
			response := strings.Replace(invokeResult, "hello", strings.Repeat("x", 70000), 1)
			response = strings.Replace(response, `"output_tokens":2`, `"output_tokens":20000`, 1)
			raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				io.WriteString(w, response)
			})
			client := NewClient(raw, WithTracerProvider(p), WithTraceConfig(TraceConfig{HideOutputs: hide}))
			out, err := client.InvokeModel(context.Background(), &bedrockruntime.InvokeModelInput{
				ModelId: aws.String(model), Body: []byte(invokeBody), ContentType: aws.String("application/json"),
			})
			if err != nil || string(out.Body) != response {
				t.Fatal("SDK response not preserved", err)
			}
			spans := llms(e)
			if len(spans) != 1 {
				t.Fatalf("want one semantic span, got %d", len(spans))
			}
			a := attrs(spans[0])
			for k, want := range map[string]int64{"input_tokens": 0, "output_tokens": 20000, "cache_read_tokens": 3, "cache_write_tokens": 4} {
				got := a["gen_ai.usage."+k]
				if got.Type() != attribute.INT64 || got.AsInt64() != want {
					t.Errorf("response %d bytes, hide=%v: service %s=%d lost (got %v)", len(response), hide, k, want, got)
				}
			}
			if got := a["gen_ai.response.finish_reasons"].AsStringSlice(); len(got) != 1 || got[0] != "tool_use" {
				t.Error("authoritative stop reason lost")
			}
			if !hide && !a["traceai.capture.truncated"].AsBool() {
				t.Error("missing truncation marker")
			}
			data, _ := json.Marshal(spans)
			for _, secret := range []string{strings.Repeat("x", 64), "thinking-canary", "signature-canary", "Paris"} {
				if strings.Contains(string(data), secret) {
					t.Error("uncaptured response content leaked")
				}
			}
			if hide && a["output.value"].AsString() != "__REDACTED__" {
				t.Error("hidden output not redacted")
			}
		})
	}
}

func TestLargeInvokeMetadataValidation(t *testing.T) {
	large := strings.Repeat("x", 1<<20)
	for _, tc := range []struct {
		name, body string
		output     bool
		stop       bool
	}{
		{"after-content", `{"content":[{"text":"` + large + `"}],"usage":{"output_tokens":0},"stop_reason":"end_turn"}`, true, true},
		{"before-content", `{"usage":{"output_tokens":0},"stop_reason":"end_turn","content":[{"text":"` + large + `"}]}`, true, true},
		{"unknown-large-fields", `{"unknown":{"usage":{"output_tokens":99},"text":"` + large + `"},"usage":{"output_tokens":0,"unknown":"` + large + `"},"stop_reason":"end_turn"}`, true, true},
		{"missing", `{"content":[{"text":"` + large + `"}]}`, false, false},
		{"negative", `{"content":[{"text":"` + large + `"}],"usage":{"output_tokens":-1}}`, false, false},
		{"malformed", `{"content":[{"text":"` + large + `"}],"usage":{"output_tokens":0}`, false, false},
		{"huge-stop", `{"content":[],"stop_reason":"` + large + `","usage":{"output_tokens":0}}`, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p, e := provider(t)
			_, r := NewClient(nil, WithTracerProvider(p)).start(context.Background(), "InvokeModel", nil)
			r.invokeOutput([]byte(tc.body))
			r.finish(nil, false)
			a := attrs(llms(e)[0])
			_, present := a["gen_ai.usage.output_tokens"]
			if present != tc.output || (present && a["gen_ai.usage.output_tokens"].AsInt64() != 0) {
				t.Fatal("usage validation", a)
			}
			_, present = a["gen_ai.response.finish_reasons"]
			if present != tc.stop {
				t.Fatal("stop reason validation", a)
			}
			if len(r.blocks) != 0 {
				t.Fatal("retained response content")
			}
		})
	}
}

// Run with -benchmem: ignored completion content must not be copied into a
// RawMessage/map/string simply to extract metadata, including when hidden.
func BenchmarkLargeInvokeMetadata(b *testing.B) {
	for _, size := range []int{70000, 1 << 20} {
		name := "70KiB"
		if size == 1<<20 {
			name = "1MiB"
		}
		b.Run(name, func(b *testing.B) {
			data := []byte(strings.Replace(invokeResult, "hello", strings.Repeat("x", size), 1))
			r := &record{span: trace.SpanFromContext(context.Background()), privacy: TraceConfig{HideOutputs: true}}
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				r.invokeOutput(data)
			}
		})
	}
}
