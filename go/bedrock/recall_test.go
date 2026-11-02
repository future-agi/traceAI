package traceaibedrock

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"unicode/utf8"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/document"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdk "go.opentelemetry.io/otel/sdk/trace"
)

func TestRecallInvalidUTF8DoesNotPoisonOTLP(t *testing.T) {
	p, e := provider(t)
	var awsReceived atomic.Bool
	raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		if !json.Valid(b) || !utf8.Valid(b) {
			t.Error("invalid AWS JSON")
		}
		if !strings.Contains(string(b), `�`) {
			t.Error("AWS did not replace invalid UTF-8")
		}
		awsReceived.Store(true)
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, syncBody)
	})
	in := input()
	in.Messages[0].Content = []types.ContentBlock{&types.ContentBlockMemberText{Value: "valid prefix " + string([]byte{0xff}) + " valid suffix"}}
	_, err := NewClient(raw, WithTracerProvider(p)).Converse(context.Background(), in)
	if err != nil || !awsReceived.Load() {
		t.Fatalf("AWS operation failed: %v", err)
	}
	var received atomic.Int32
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received.Add(1)
		io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	defer receiver.Close()
	exporter, err := otlptracehttp.New(context.Background(), otlptracehttp.WithEndpointURL(receiver.URL), otlptracehttp.WithRetry(otlptracehttp.RetryConfig{Enabled: false}))
	if err != nil {
		t.Fatal(err)
	}
	defer exporter.Shutdown(context.Background())
	spans := []sdk.ReadOnlySpan{}
	for _, s := range e.GetSpans() {
		spans = append(spans, s.Snapshot())
		for _, a := range s.Attributes {
			if a.Value.Type() == attribute.STRING && !utf8.ValidString(a.Value.AsString()) {
				t.Errorf("invalid UTF-8 retained in %s", a.Key)
			}
		}
	}
	if err := exporter.ExportSpans(context.Background(), spans); err != nil {
		t.Fatalf("successful AWS operation cannot export OTLP: %v; receiver requests=%d", err, received.Load())
	}
	if received.Load() != 1 {
		t.Fatalf("OTLP requests = %d, want 1", received.Load())
	}
}

func TestRecallCanonicalStopSequences(t *testing.T) {
	p, e := provider(t)
	raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(b), `"stopSequences":["STOP_ONE","STOP_TWO"]`) {
			t.Errorf("stop sequences not sent by AWS SDK: %s", b)
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, syncBody)
	})
	in := input()
	in.InferenceConfig.StopSequences = []string{"STOP_ONE", "STOP_TWO"}
	if _, err := NewClient(raw, WithTracerProvider(p)).Converse(context.Background(), in); err != nil {
		t.Fatal(err)
	}
	a := attrs(llms(e)[0])
	if v := a["gen_ai.request.stop_sequences"]; v.Type() != attribute.STRINGSLICE || !reflect.DeepEqual(v.AsStringSlice(), []string{"STOP_ONE", "STOP_TWO"}) {
		t.Fatalf("canonical stop sequence array missing: got %v; noncanonical .0=%q .1=%q", v, a["gen_ai.request.stop_sequences.0"].AsString(), a["gen_ai.request.stop_sequences.1"].AsString())
	}
}

func TestRecallSemanticStreamCompletion(t *testing.T) {
	for _, op := range []string{"converse", "invoke"} {
		for _, hidden := range []bool{false, true} {
			for _, complete := range []bool{false, true} {
				for _, empty := range []bool{false, true} {
					t.Run(fmt.Sprintf("%s/hidden=%t/complete=%t/empty=%t", op, hidden, complete, empty), func(t *testing.T) {
						p, e := provider(t)
						raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
							w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
							if op == "converse" {
								if !empty {
									frame(w, "messageStart", `{"role":"assistant"}`)
									frame(w, "contentBlockDelta", `{"contentBlockIndex":0,"delta":{"text":"partial answer"}}`)
								}
								if complete {
									frame(w, "messageStop", `{"stopReason":"end_turn"}`)
								}
							} else {
								var payloads []string
								if !empty {
									payloads = append(payloads, `{"type":"message_start","message":{"usage":{"input_tokens":2}}}`, `{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial answer"}}`, `{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}`)
								}
								if complete {
									payloads = append(payloads, `{"type":"message_stop"}`)
								}
								for _, payload := range payloads {
									b, _ := json.Marshal(map[string]string{"bytes": base64.StdEncoding.EncodeToString([]byte(payload))})
									frame(w, "chunk", string(b))
								}
							}
							// End at a valid frame boundary: the real SDK reports nil Err().
						})
						c := NewClient(raw, WithTracerProvider(p), WithTraceConfig(TraceConfig{HideOutputs: hidden}))
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
						want := 0
						if !empty {
							want = 2
							if op == "invoke" {
								want++
							}
						}
						if complete {
							want++
						}
						if count != want || streamErr != nil || closeErr != nil {
							t.Fatalf("SDK contract changed: events=%d want=%d Err=%v Close=%v", count, want, streamErr, closeErr)
						}
						spans := llms(e)
						if len(spans) != 1 {
							t.Fatalf("span count = %d", len(spans))
						}
						s := spans[0]
						a := attrs(s)
						if complete {
							if s.Status.Code != codes.Unset || a["error.type"].Type() != attribute.INVALID || a["traceai.stream.closed_early"].Type() != attribute.INVALID {
								t.Fatalf("complete stream marked unsuccessful: %v %v", s.Status, a)
							}
						} else if s.Status.Code != codes.Error || a["error.type"].AsString() != "incomplete_stream" || !a["traceai.stream.closed_early"].AsBool() {
							t.Fatalf("incomplete stream lacks error telemetry: %v %v", s.Status, a)
						}
						if hidden {
							if a["output.value"].AsString() != "__REDACTED__" {
								t.Fatal("hidden output leaked")
							}
						} else if !empty && a["gen_ai.output.messages.0.message.content"].AsString() != "partial answer" {
							t.Fatal("partial output lost")
						}
					})
				}
			}
		}
	}
}

func TestRecallMultipleToolResultsKeepAssociations(t *testing.T) {
	for _, op := range []string{"converse", "invoke"} {
		t.Run(op, func(t *testing.T) {
			p, e := provider(t)
			raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
				body, _ := io.ReadAll(r.Body)
				for _, want := range []string{"call-a", "call-b", "RESULT_A", "RESULT_B"} {
					if !strings.Contains(string(body), want) {
						t.Errorf("request lost %s", want)
					}
				}
				w.Header().Set("Content-Type", "application/json")
				if op == "converse" {
					io.WriteString(w, syncBody)
				} else {
					io.WriteString(w, invokeResult)
				}
			})
			c := NewClient(raw, WithTracerProvider(p))
			if op == "converse" {
				in := input()
				in.System = nil
				in.Messages = []types.Message{
					{Role: types.ConversationRoleUser, Content: []types.ContentBlock{&types.ContentBlockMemberText{Value: "Compare two cities' weather"}}},
					{Role: types.ConversationRoleAssistant, Content: []types.ContentBlock{
						&types.ContentBlockMemberToolUse{Value: types.ToolUseBlock{ToolUseId: aws.String("call-a"), Name: aws.String("weather"), Input: document.NewLazyDocument(map[string]string{"city": "Paris"})}},
						&types.ContentBlockMemberToolUse{Value: types.ToolUseBlock{ToolUseId: aws.String("call-b"), Name: aws.String("weather"), Input: document.NewLazyDocument(map[string]string{"city": "London"})}},
					}},
					{Role: types.ConversationRoleUser, Content: []types.ContentBlock{
						&types.ContentBlockMemberToolResult{Value: types.ToolResultBlock{ToolUseId: aws.String("call-a"), Content: []types.ToolResultContentBlock{&types.ToolResultContentBlockMemberText{Value: "RESULT_A"}}}},
						&types.ContentBlockMemberToolResult{Value: types.ToolResultBlock{ToolUseId: aws.String("call-b"), Content: []types.ToolResultContentBlock{&types.ToolResultContentBlockMemberText{Value: "RESULT_B"}}}},
					}},
				}
				if _, err := c.Converse(context.Background(), in); err != nil {
					t.Fatal(err)
				}
			} else {
				body := `{"anthropic_version":"bedrock-2023-05-31","max_tokens":100,"tools":[{"name":"weather","input_schema":{"type":"object"}}],"messages":[{"role":"user","content":"Compare two cities' weather"},{"role":"assistant","content":[{"type":"tool_use","id":"call-a","name":"weather","input":{"city":"Paris"}},{"type":"tool_use","id":"call-b","name":"weather","input":{"city":"London"}}]},{"role":"user","content":[{"type":"tool_result","tool_use_id":"call-a","content":"RESULT_A"},{"type":"tool_result","tool_use_id":"call-b","content":"RESULT_B"}]}]}`
				if _, err := c.InvokeModel(context.Background(), &bedrockruntime.InvokeModelInput{ModelId: aws.String(model), Body: []byte(body), ContentType: aws.String("application/json")}); err != nil {
					t.Fatal(err)
				}
			}
			a := attrs(llms(e)[0])
			for i, want := range []struct{ id, text string }{{"call-a", "RESULT_A"}, {"call-b", "RESULT_B"}} {
				prefix := fmt.Sprintf("gen_ai.input.messages.%d.message.", i+2)
				if a[prefix+"role"].AsString() != "tool" || a[prefix+"tool_call_id"].AsString() != want.id || a[prefix+"content"].AsString() != want.text {
					t.Fatalf("tool result association lost at %s: %v", prefix, a)
				}
			}
			var summary []message
			if err := json.Unmarshal([]byte(a["input.value"].AsString()), &summary); err != nil || len(summary) != 3 || len(summary[2].Content) != 2 || summary[2].Content[0].ID != "call-a" || summary[2].Content[1].ID != "call-b" {
				t.Fatalf("provider summary changed: %s", a["input.value"].AsString())
			}
		})
	}
}
