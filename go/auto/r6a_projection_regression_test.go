package auto

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"google.golang.org/protobuf/proto"
)

// 20 bytes of headerless 16-bit PCM: no magic bytes, so only its MIME sibling
// identifies it as audio (go-s6 final #1).
const r6aPCM = "AQIDBAUGBwgJCgsMDQ4PEBESExQ="

func r6aLongChat(t *testing.T, ctx context.Context, messages int) {
	t.Helper()
	history := make([]map[string]string, messages)
	for i := range history {
		history[i] = map[string]string{"role": "user", "content": fmt.Sprintf("message %d", i)}
	}
	body, _ := json.Marshal(map[string]any{"model": "m", "messages": history})
	const reply = `{"id":"c1","model":"m","choices":[{"message":{"role":"assistant","content":"VISIBLE_OUTPUT"},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}`
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		if !bytes.Equal(data, body) {
			t.Error("changed request")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, reply)
	}))
	defer provider.Close()
	req, _ := http.NewRequestWithContext(ctx, "POST", provider.URL+"/v1/chat/completions", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	data, readErr := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if readErr != nil || string(data) != reply {
		t.Fatal("agent changed the application response")
	}
}

// go-s6 final #1 / A7: projection never separates MIME context from its
// payload, and headerless audio identified only by that context never reaches
// the host view or the OTLP export, at the default and smaller host limits.
func TestR6aProjectionKeepsMediaContextOTLP(t *testing.T) {
	// Sweep limits so the edge with exactly one free metadata slot (the
	// verifier's leak: payload admitted, MIME sibling dropped) is always hit.
	// On the pre-fix code it recurred at every odd limit (21..135) for the
	// long chat; TestR6aProjectionMediaCompanions sweeps every budget limit.
	limits := []int{63, 64, 127, 128, 129, 135, 136}
	for limit := 16; limit <= 40; limit++ {
		limits = append(limits, limit)
	}
	for _, limit := range limits {
		for _, messages := range []int{2, 80} {
			t.Run(fmt.Sprintf("limit=%d/messages=%d", limit, messages), func(t *testing.T) {
				exporterRegressionSetup(t)
				received := privacyCollector(t)
				limits := sdktrace.NewSpanLimits()
				limits.AttributeCountLimit = limit
				host := tracetest.NewSpanRecorder()
				p := sdktrace.NewTracerProvider(sdktrace.WithRawSpanLimits(limits), sdktrace.WithSpanProcessor(host))
				defer p.Shutdown(context.Background())
				if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("R6A_MEDIA_FI_KEY", "R6A_MEDIA_FI_SECRET")); err != nil {
					t.Fatal(err)
				}
				ctx := ContextWithUserID(ContextWithSessionID(context.Background(), "session-1"), "user-1")
				ctx = ContextWithAttributes(ctx, attribute.String("z.record.mimeType", "audio/L16"), attribute.String("z.record.data", r6aPCM))
				r6aLongChat(t, ctx, messages)
				request := flushedPrivacyExport(t, received)
				wire, _ := proto.Marshal(request)
				if bytes.Contains(wire, []byte(r6aPCM)) {
					t.Fatal("raw PCM reached the OTLP export")
				}
				if len(host.Ended()) != 1 {
					t.Fatal("host provider lost the wire span")
				}
				for _, kv := range host.Ended()[0].Attributes() {
					if strings.Contains(kv.Value.Emit(), r6aPCM) {
						t.Fatalf("raw PCM reached the host view through %s", kv.Key)
					}
				}
				attrs := s6ProtoAttrs(request.ResourceSpans[0].ScopeSpans[0].Spans[0])
				if attrs["z.record.data"] != nil && attrs["z.record.mimeType"] == nil {
					t.Fatal("projection separated the MIME hint from its payload")
				}
				for _, key := range []string{"gen_ai.span.kind", "gen_ai.provider.name", "gen_ai.request.model", "gen_ai.response.id", "http.response.status_code", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens", "traceai.capture.layer", "user.id", "session.id"} {
					if attrs[key] == nil {
						t.Errorf("limit %d lost protected %s", limit, key)
					}
				}
				if messages == 80 && (attrs["traceai.capture.dropped_content_count"] == nil || attrs["traceai.wire.attributes_truncated"] == nil) {
					t.Error("degradation markers missing")
				}
				if limit >= 32 && attrs["output.value"] == nil {
					t.Error("bounded output did not win over input")
				}
			})
		}
	}
}

// The projection pins every MIME/type hint to each group holding a key under
// the hint's parent, deterministically, whatever the limit or input order.
func TestR6aProjectionMediaCompanions(t *testing.T) {
	base := []attribute.KeyValue{attribute.String("gen_ai.span.kind", "LLM"), attribute.String("traceai.capture.layer", "wire"), attribute.Int64("gen_ai.usage.total_tokens", 3), attribute.String("output.value", "ok"), attribute.String("output.mime_type", "text/plain")}
	for i := 0; i < 6; i++ {
		base = append(base, attribute.String(fmt.Sprintf("gen_ai.input.messages.%d.message.role", i), "user"), attribute.String(fmt.Sprintf("gen_ai.input.messages.%d.message.content", i), "text"))
	}
	cases := map[string][]attribute.KeyValue{
		"nested":  {attribute.String("z.record.mimeType", "audio/L16"), attribute.String("z.record.data", "<omitted: media>"), attribute.String("z.record.bytes.0", "x")},
		"indexed": {attribute.StringSlice("a.media.mime_type", []string{"image/png"}), attribute.String("a.media.data.0", "y")},
		"root":    {attribute.String("mime_type", "audio/wav"), attribute.String("data", "z")},
		"type":    {attribute.String("v.part.type", "input_audio"), attribute.String("v.part.audio.data", "w")},
	}
	for name, extra := range cases {
		values := append(append([]attribute.KeyValue(nil), base...), extra...)
		hint, parent := "", ""
		for _, kv := range extra {
			if p, ok := mediaHintParent(kv); ok {
				hint, parent = string(kv.Key), p
			}
		}
		if hint == "" {
			t.Fatalf("%s: no catalog hint recognized", name)
		}
		for limit := 0; limit <= len(values)+2; limit++ {
			got := budgetAttributes(values, limit)
			reversed := append([]attribute.KeyValue(nil), values...)
			for i, j := 0, len(reversed)-1; i < j; i, j = i+1, j-1 {
				reversed[i], reversed[j] = reversed[j], reversed[i]
			}
			if !reflect.DeepEqual(got, budgetAttributes(reversed, limit)) || len(got) > limit {
				t.Fatalf("%s/%d: nondeterministic or over-budget projection", name, limit)
			}
			kept := map[string]bool{}
			for _, kv := range got {
				kept[string(kv.Key)] = true
			}
			for _, kv := range values {
				key := string(kv.Key)
				if key != hint && kept[key] && protectedAttributePriority(kv) < 0 && (parent == "" || strings.HasPrefix(key, parent+".")) && !kept[hint] {
					t.Fatalf("%s/%d: %s admitted without its MIME context %s", name, limit, key, hint)
				}
			}
		}
	}
}
