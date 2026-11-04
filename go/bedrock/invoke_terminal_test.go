package traceaibedrock

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
)

func TestInvokeTerminalDetectionSkipsContent(t *testing.T) {
	large, err := json.Marshal(strings.Repeat("private-trace-canary", 4000))
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, payload string
		complete      bool
	}{
		{"type-first", `{"type":"message_stop","trace":` + string(large) + `}`, true},
		{"type-last", `{"trace":` + string(large) + `,"type":"message_stop"}`, true},
		{"escaped-type", `{"trace":` + string(large) + `,"type":"message_stop"}`, true},
		{"nested-type-only", `{"trace":` + string(large) + `,"nested":{"type":"message_stop"}}`, false},
		{"nonterminal", `{"type":"content_block_delta","delta":{"type":"text_delta","text":` + string(large) + `}}`, false},
		{"malformed-tail", `{"type":"message_stop","trace":` + string(large), false},
		{"trailing-json", `{"type":"message_stop","trace":` + string(large) + `}{}`, false},
		{"oversized-type", `{"type":` + string(large) + `}`, false},
	} {
		for name, privacy := range map[string]TraceConfig{
			"visible": {}, "outputs": {HideOutputs: true},
			"messages": {HideOutputMessages: true}, "text": {HideOutputText: true},
		} {
			t.Run(tc.name+"/"+name, func(t *testing.T) {
				for _, used := range []int{0, maxCapture} {
					r := &record{privacy: privacy, used: used, blocks: make(map[int32]*block)}
					r.invokeEvent(&types.ResponseStreamMemberChunk{Value: types.PayloadPart{Bytes: []byte(tc.payload)}})
					if r.streamComplete != tc.complete {
						t.Fatalf("streamComplete=%t, want %t", r.streamComplete, tc.complete)
					}
					if r.used != used || len(r.blocks) != 0 {
						t.Fatal("oversized metadata or content retained")
					}
				}
			})
		}
	}
}

func TestInvokeLargeTerminalMetadata(t *testing.T) {
	for _, hidden := range []bool{false, true} {
		for _, size := range []int{16, 70 << 10} {
			t.Run(fmt.Sprintf("hide=%t/metadata=%d", hidden, size), func(t *testing.T) {
				p, exporter := provider(t)
				// Bedrock documents detailed guardrail tracing on the last chunk.
				// It is service metadata, not text that the instrumentor should capture.
				terminal, err := json.Marshal(map[string]any{
					"type":                             "message_stop",
					"amazon-bedrock-invocationMetrics": map[string]int{"inputTokenCount": 2, "outputTokenCount": 2},
					"amazon-bedrock-trace": map[string]any{"guardrail": map[string]any{
						"outputs": []any{map[string]any{"sensitiveInformationPolicy": map[string]any{
							"piiEntities": []any{map[string]string{"type": "NAME", "action": "ANONYMIZED", "match": strings.Repeat("private-trace-canary", size/20+1)}},
						}}},
					}},
				})
				if err != nil {
					t.Fatal(err)
				}
				raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
					for _, payload := range []string{
						`{"type":"message_start","message":{"usage":{"input_tokens":2}}}`,
						`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"complete answer"}}`,
						`{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}`,
						string(terminal),
					} {
						chunk, _ := json.Marshal(map[string]string{"bytes": base64.StdEncoding.EncodeToString([]byte(payload))})
						if err := frame(w, "chunk", string(chunk)); err != nil {
							t.Error(err)
						}
					}
				})
				client := NewClient(raw, WithTracerProvider(p), WithTraceConfig(TraceConfig{HideOutputs: hidden}))
				out, err := client.InvokeModelWithResponseStream(context.Background(), &bedrockruntime.InvokeModelWithResponseStreamInput{
					ModelId: aws.String(model), Body: []byte(invokeBody), ContentType: aws.String("application/json"),
				})
				if err != nil {
					t.Fatal(err)
				}
				count, stops := 0, 0
				for event := range out.GetStream().Events() {
					count++
					chunk := event.(*types.ResponseStreamMemberChunk)
					var decoded struct {
						Type string `json:"type"`
					}
					if err := json.Unmarshal(chunk.Value.Bytes, &decoded); err != nil {
						t.Fatal(err)
					}
					if decoded.Type == "message_stop" {
						stops++
						if string(chunk.Value.Bytes) != string(terminal) {
							t.Error("terminal bytes changed")
						}
					}
				}
				if err := out.GetStream().Err(); err != nil {
					t.Fatal(err)
				}
				if err := out.GetStream().Close(); err != nil {
					t.Fatal(err)
				}
				if count != 4 || stops != 1 {
					t.Fatalf("events=%d, terminal events=%d", count, stops)
				}
				spans := llms(exporter)
				if len(spans) != 1 {
					t.Fatalf("span count=%d", len(spans))
				}
				span := spans[0]
				a := attrs(span)
				serialized, _ := json.Marshal(spans)
				if strings.Contains(string(serialized), "private-trace-canary") {
					t.Fatal("guardrail trace leaked")
				}
				if span.Status.Code == codes.Error || a["error.type"].Type() != attribute.INVALID || a["traceai.stream.closed_early"].AsBool() {
					t.Fatalf("complete native SDK stream misclassified: terminal bytes=%d, events=%d, message_stop=%d, native Err=nil, status=%v, error.type=%q, closed_early=%t", len(terminal), count, stops, span.Status, a["error.type"].AsString(), a["traceai.stream.closed_early"].AsBool())
				}
			})
		}
	}
}
