package traceaibedrock

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"go.opentelemetry.io/otel/codes"
)

func TestTakeUTF8NormalizationBudget(t *testing.T) {
	r := &record{used: maxCapture - 2}
	if got := r.take("a\xff"); got != "a" || r.used != maxCapture-1 || !r.truncated {
		t.Fatalf("normalization must mark truncation and charge retained bytes: got=%q used=%d truncated=%t", got, r.used, r.truncated)
	}
	if got := r.take("z"); got != "z" || r.used != maxCapture {
		t.Fatal("remaining byte budget was lost")
	}
	if got := r.take("more"); got != "" || r.used != maxCapture {
		t.Fatal("capture exceeded the byte budget")
	}
}

func TestToolResultPrivacyAndBudgets(t *testing.T) {
	for _, op := range []string{"converse", "invoke"} {
		for name, privacy := range map[string]TraceConfig{
			"visible": {}, "inputs": {HideInputs: true},
			"messages": {HideInputMessages: true}, "text": {HideInputText: true},
		} {
			for _, size := range []int{1, 4000} {
				t.Run(fmt.Sprintf("%s/%s/size=%d", op, name, size), func(t *testing.T) {
					p, e := provider(t)
					_, r := NewClient(nil, WithTracerProvider(p), WithTraceConfig(privacy)).start(context.Background(), "test", nil)
					var content []types.ContentBlock
					var invokeContent []map[string]string
					for i := 0; i < 20; i++ {
						id, text := fmt.Sprintf("call-%d", i), strings.Repeat("R", size)
						content = append(content, &types.ContentBlockMemberToolResult{Value: types.ToolResultBlock{
							ToolUseId: aws.String(id), Content: []types.ToolResultContentBlock{&types.ToolResultContentBlockMemberText{Value: text}},
						}})
						invokeContent = append(invokeContent, map[string]string{"type": "tool_result", "tool_use_id": id, "content": text})
					}
					if op == "converse" {
						r.converseInput([]types.Message{{Role: types.ConversationRoleUser, Content: content}}, nil, nil, nil)
					} else {
						data, _ := json.Marshal(invokeContent)
						r.invokeInput(&invocation{Messages: []invokeMessage{{Role: "user", Content: data}}})
					}
					r.finish(nil, false)
					s := llms(e)[0]
					a := attrs(s)
					captured := 0
					for i := 0; i < 20; i++ {
						prefix := fmt.Sprintf("gen_ai.input.messages.%d.message.", i)
						id, text := a[prefix+"tool_call_id"].AsString(), a[prefix+"content"].AsString()
						if !privacy.input() && (id != "" || text != "") {
							t.Fatal("hidden tool result leaked")
						}
						if text != "" && (id != fmt.Sprintf("call-%d", i) || a[prefix+"role"].AsString() != "tool") {
							t.Fatal("bounded content associated with wrong result")
						}
						if !utf8.ValidString(id + text) {
							t.Fatal("invalid UTF-8 result")
						}
						captured += len(id) + len(text)
					}
					if privacy.input() && (captured == 0 || captured > maxCapture || !a["traceai.capture.truncated"].AsBool() || s.DroppedAttributes != 0) {
						t.Fatalf("capture budgets changed: captured=%d dropped=%d", captured, s.DroppedAttributes)
					}
					if !privacy.input() {
						encoded, _ := json.Marshal(s)
						if strings.Contains(string(encoded), "call-") {
							t.Fatal("tool identifier leaked through another attribute")
						}
					}
				})
			}
		}
	}
}

func TestSemanticCompletionAfterCaptureExhausted(t *testing.T) {
	for _, op := range []string{"converse", "invoke"} {
		for _, complete := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/complete=%t", op, complete), func(t *testing.T) {
				p, e := provider(t)
				text := strings.Repeat("x", maxCapture+512)
				raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
					if op == "converse" {
						frame(w, "contentBlockDelta", `{"contentBlockIndex":0,"delta":{"text":"`+text+`"}}`)
						if complete {
							frame(w, "messageStop", `{"stopReason":"end_turn"}`)
						}
					} else {
						payloads := []string{`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"` + text + `"}}`}
						if complete {
							payloads = append(payloads, `{"type":"message_stop"}`)
						}
						for _, payload := range payloads {
							chunk, _ := json.Marshal(map[string]string{"bytes": base64.StdEncoding.EncodeToString([]byte(payload))})
							frame(w, "chunk", string(chunk))
						}
					}
				})
				c := NewClient(raw, WithTracerProvider(p))
				count := 0
				var streamErr, closeErr error
				if op == "converse" {
					out, err := c.ConverseStream(context.Background(), &bedrockruntime.ConverseStreamInput{ModelId: aws.String(model), Messages: input().Messages})
					if err != nil {
						t.Fatal(err)
					}
					for range out.GetStream().Events() {
						count++
					}
					streamErr, closeErr = out.GetStream().Err(), out.GetStream().Close()
				} else {
					out, err := c.InvokeModelWithResponseStream(context.Background(), &bedrockruntime.InvokeModelWithResponseStreamInput{ModelId: aws.String(model), Body: []byte(invokeBody)})
					if err != nil {
						t.Fatal(err)
					}
					for range out.GetStream().Events() {
						count++
					}
					streamErr, closeErr = out.GetStream().Err(), out.GetStream().Close()
				}
				want := 1
				if complete {
					want++
				}
				if count != want || streamErr != nil || closeErr != nil {
					t.Fatalf("SDK contract changed: events=%d Err=%v Close=%v", count, streamErr, closeErr)
				}
				s := llms(e)[0]
				a := attrs(s)
				if !a["traceai.capture.truncated"].AsBool() || len(a["gen_ai.output.messages.0.message.content"].AsString()) != maxCapture {
					t.Fatal("expected exhausted content budget")
				}
				if (s.Status.Code == codes.Error) == complete || a["traceai.stream.closed_early"].AsBool() == complete {
					t.Fatal("content budget affected semantic completion")
				}
			})
		}
	}
}
