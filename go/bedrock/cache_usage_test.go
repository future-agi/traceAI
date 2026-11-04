package traceaibedrock

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"go.opentelemetry.io/otel/attribute"
)

// Equal aggregate writes can have different costs. Exercise all four operations
// through the real SDK, including its HTTP and event-stream decoders, rather than
// calling the record's usage helpers directly.
func TestCacheWriteTTLWire(t *testing.T) {
	for _, op := range []string{"Converse", "ConverseStream", "InvokeModel", "InvokeModelWithResponseStream"} {
		for name, privacy := range map[string]TraceConfig{
			"visible":         {},
			"hidden":          {HideInputs: true, HideOutputs: true},
			"hidden-messages": {HideInputMessages: true, HideOutputMessages: true},
			"hidden-text":     {HideInputText: true, HideOutputText: true},
		} {
			t.Run(op+"/"+name, func(t *testing.T) {
				p, e := provider(t)
				var requests atomic.Int32
				raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
					if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256") {
						t.Error("request was not signed by the AWS SDK")
					}
					oneHour, fiveMinutes := 900, 100
					if requests.Add(1) == 2 {
						oneHour, fiveMinutes = 100, 900
					}
					usage := fmt.Sprintf(`{"inputTokens":10,"outputTokens":5,"totalTokens":1015,"cacheReadInputTokens":0,"cacheWriteInputTokens":1000,"cacheDetails":[{"inputTokens":%d,"ttl":"1h"},{"inputTokens":%d,"ttl":"5m"}]}`, oneHour, fiveMinutes)
					invokeUsage := fmt.Sprintf(`{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":0,"cache_creation_input_tokens":1000,"cache_creation":{"ephemeral_1h_input_tokens":%d,"ephemeral_5m_input_tokens":%d}}`, oneHour, fiveMinutes)
					switch op {
					case "Converse":
						w.Header().Set("Content-Type", "application/json")
						io.WriteString(w, `{"output":{"message":{"role":"assistant","content":[{"text":"cache-output-canary"}]}},"stopReason":"end_turn","usage":`+usage+`}`)
					case "ConverseStream":
						w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
						writeCacheFrame(t, w, "contentBlockDelta", `{"contentBlockIndex":0,"delta":{"text":"cache-output-canary"}}`)
						writeCacheFrame(t, w, "messageStop", `{"stopReason":"end_turn"}`)
						writeCacheFrame(t, w, "metadata", `{"usage":`+usage+`,"metrics":{"latencyMs":1}}`)
					case "InvokeModel":
						w.Header().Set("Content-Type", "application/json")
						io.WriteString(w, `{"content":[{"type":"text","text":"cache-output-canary"}],"stop_reason":"end_turn","usage":`+invokeUsage+`}`)
					default:
						w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
						for _, payload := range []string{
							`{"type":"message_start","message":{"usage":` + invokeUsage + `}}`,
							`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"cache-output-canary"}}`,
							// Repeated usage is a snapshot, not additional cache writes.
							`{"type":"message_delta","usage":` + invokeUsage + `}`,
							// A later output-only snapshot must retain both TTL counts.
							`{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}`,
							`{"type":"message_stop"}`,
						} {
							body, err := json.Marshal(map[string]string{"bytes": base64.StdEncoding.EncodeToString([]byte(payload))})
							if err != nil {
								t.Error(err)
								return
							}
							writeCacheFrame(t, w, "chunk", string(body))
						}
					}
				})
				c := NewClient(raw, WithTracerProvider(p), WithTraceConfig(privacy))
				for call := 0; call < 2; call++ {
					ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
					defer cancel()
					if op == "Converse" {
						out, err := c.Converse(ctx, input())
						if err != nil {
							t.Fatal(err)
						}
						want := int32(900)
						if call == 1 {
							want = 100
						}
						if out.Usage == nil || len(out.Usage.CacheDetails) != 2 || out.Usage.CacheDetails[0].Ttl != types.CacheTTLOneHour || out.Usage.CacheDetails[0].InputTokens == nil || *out.Usage.CacheDetails[0].InputTokens != want {
							t.Fatal("native SDK response lost cache details")
						}
					} else {
						callCacheOperation(t, ctx, c, op)
					}
					cancel()
				}
				spans := llms(e)
				if len(spans) != 2 || requests.Load() != 2 {
					t.Fatalf("spans=%d requests=%d", len(spans), requests.Load())
				}
				for i, s := range spans {
					a := attrs(s)
					oneHour, fiveMinutes := int64(900), int64(100)
					if i == 1 {
						oneHour, fiveMinutes = 100, 900
					}
					for key, want := range map[string]int64{
						"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
						"cache_write_tokens": 1000, "cache_write_1h_tokens": oneHour, "cache_write_5m_tokens": fiveMinutes,
					} {
						assertCacheCounter(t, a, key, &want)
					}
					if strings.HasPrefix(op, "Converse") {
						want := int64(1015)
						assertCacheCounter(t, a, "total_tokens", &want)
					} else {
						assertCacheCounter(t, a, "total_tokens", nil)
					}
					if name != "visible" {
						encoded, err := json.Marshal(s)
						if err != nil {
							t.Fatal(err)
						}
						if strings.Contains(string(encoded), "cache-output-canary") {
							t.Fatal("hidden content leaked")
						}
						for key := range a {
							if strings.HasPrefix(key, "gen_ai.input.messages") || strings.HasPrefix(key, "gen_ai.output.messages") {
								t.Fatalf("hidden message attribute %s", key)
							}
						}
					}
					if name == "hidden" && (a["input.value"].AsString() != "__REDACTED__" || a["output.value"].AsString() != "__REDACTED__") {
						t.Fatal("redaction markers missing")
					}
				}
			})
		}
	}
}

func writeCacheFrame(t *testing.T, w io.Writer, event, payload string) {
	t.Helper()
	if err := frame(w, event, payload); err != nil {
		t.Error(err)
	}
}

func callCacheOperation(t *testing.T, ctx context.Context, c *Client, op string) {
	t.Helper()
	switch op {
	case "ConverseStream":
		out, err := c.ConverseStream(ctx, &bedrockruntime.ConverseStreamInput{ModelId: aws.String(model), Messages: input().Messages})
		if err != nil {
			t.Fatal(err)
		}
		s := out.GetStream()
		defer s.Close()
		for range s.Events() {
		}
		if err := s.Err(); err != nil {
			t.Fatal(err)
		}
	case "InvokeModel":
		_, err := c.InvokeModel(ctx, &bedrockruntime.InvokeModelInput{ModelId: aws.String(model), Body: []byte(invokeBody), ContentType: aws.String("application/json")})
		if err != nil {
			t.Fatal(err)
		}
	case "InvokeModelWithResponseStream":
		out, err := c.InvokeModelWithResponseStream(ctx, &bedrockruntime.InvokeModelWithResponseStreamInput{ModelId: aws.String(model), Body: []byte(invokeBody), ContentType: aws.String("application/json")})
		if err != nil {
			t.Fatal(err)
		}
		s := out.GetStream()
		defer s.Close()
		for range s.Events() {
		}
		if err := s.Err(); err != nil {
			t.Fatal(err)
		}
	default:
		t.Fatalf("unknown operation %q", op)
	}
}

func assertCacheCounter(t *testing.T, a map[string]attribute.Value, key string, want *int64) {
	t.Helper()
	v, present := a["gen_ai.usage."+key]
	if want == nil {
		if present {
			t.Errorf("%s fabricated: %v", key, v)
		}
	} else if !present || v.Type() != attribute.INT64 || v.AsInt64() != *want {
		t.Errorf("%s = %v, want %d", key, v, *want)
	}
}

// Missing and negative values are not zero; unknown TTLs must not be guessed.
// Duplicate per-TTL entries within one Converse snapshot are independent writes,
// but repeated snapshots and Anthropic usage updates must not accumulate them.
func TestCacheWriteTTLValues(t *testing.T) {
	zero, ten, twenty, thirty := int64(0), int64(10), int64(20), int64(30)
	for _, tc := range []struct {
		name, converse, invoke string
		fiveMinutes, oneHour   *int64
	}{
		{"absent", `{}`, `{}`, nil, nil},
		{"empty", `{"cacheDetails":[]}`, `{"cache_creation":{}}`, nil, nil},
		{"null", `{"cacheDetails":[{"ttl":"5m","inputTokens":null},{"ttl":"1h","inputTokens":null}]}`, `{"cache_creation":{"ephemeral_5m_input_tokens":null,"ephemeral_1h_input_tokens":null}}`, nil, nil},
		{"zero", `{"cacheDetails":[{"ttl":"5m","inputTokens":0},{"ttl":"1h","inputTokens":0}]}`, `{"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":0}}`, &zero, &zero},
		{"negative", `{"cacheDetails":[{"ttl":"5m","inputTokens":-1},{"ttl":"1h","inputTokens":-2}]}`, `{"cache_creation":{"ephemeral_5m_input_tokens":-1,"ephemeral_1h_input_tokens":-2}}`, nil, nil},
		{"only-5m", `{"cacheDetails":[{"ttl":"5m","inputTokens":10}]}`, `{"cache_creation":{"ephemeral_5m_input_tokens":10}}`, &ten, nil},
		{"only-1h", `{"cacheDetails":[{"ttl":"1h","inputTokens":20}]}`, `{"cache_creation":{"ephemeral_1h_input_tokens":20}}`, nil, &twenty},
		{"unknown", `{"cacheDetails":[{"ttl":"24h","inputTokens":10},{"inputTokens":20}]}`, `{"cache_creation":{"ephemeral_24h_input_tokens":10}}`, nil, nil},
		{"multiple", `{"cacheDetails":[{"ttl":"5m","inputTokens":10},{"ttl":"5m","inputTokens":20},{"ttl":"1h","inputTokens":20},{"ttl":"5m","inputTokens":-5}]}`, `{"cache_creation":{"ephemeral_5m_input_tokens":30,"ephemeral_1h_input_tokens":20}}`, &thirty, &twenty},
	} {
		for _, mode := range []string{"converse", "invoke"} {
			t.Run(tc.name+"/"+mode, func(t *testing.T) {
				p, e := provider(t)
				_, r := NewClient(nil, WithTracerProvider(p), WithTraceConfig(TraceConfig{HideInputs: true, HideOutputs: true})).start(context.Background(), "test", nil)
				if mode == "converse" {
					var u types.TokenUsage
					if err := json.Unmarshal([]byte(tc.converse), &u); err != nil {
						t.Fatal(err)
					}
					r.setUsage(&u)
					r.setUsage(&u)
				} else {
					var u invokeUsage
					if err := json.Unmarshal([]byte(tc.invoke), &u); err != nil {
						t.Fatal(err)
					}
					r.invokeUsage(&u)
					r.invokeUsage(&u)
					var outputOnly invokeUsage
					if err := json.Unmarshal([]byte(`{"output_tokens":5,"cache_creation":{}}`), &outputOnly); err != nil {
						t.Fatal(err)
					}
					r.invokeUsage(&outputOnly)
				}
				r.finish(nil, false)
				a := attrs(llms(e)[0])
				assertCacheCounter(t, a, "cache_write_5m_tokens", tc.fiveMinutes)
				assertCacheCounter(t, a, "cache_write_1h_tokens", tc.oneHour)
				assertCacheCounter(t, a, "cache_write_tokens", nil)
				assertCacheCounter(t, a, "total_tokens", nil)
			})
		}
	}
}
