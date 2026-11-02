package traceaibedrock

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"go.opentelemetry.io/otel/attribute"
)

const invokeBody = `{"anthropic_version":"bedrock-2023-05-31","messages":[{"role":"user","content":[{"type":"text","text":"input-canary"},{"type":"image","source":{"type":"base64","data":"binary-canary"}}]}],"system":"system-canary","tools":[{"name":"weather","description":"tool-canary","input_schema":{"type":"object"}}],"max_tokens":100,"temperature":0}`
const invokeResult = `{"content":[{"type":"text","text":"hello"},{"type":"tool_use","id":"c1","name":"weather","input":{"city":"Paris"}},{"type":"thinking","thinking":"thinking-canary","signature":"signature-canary"}],"stop_reason":"tool_use","usage":{"input_tokens":0,"output_tokens":2,"cache_read_input_tokens":3,"cache_creation_input_tokens":4}}`

func invokeFrames(w io.Writer) {
	for _, payload := range []string{`{"type":"message_start","message":{"usage":{"input_tokens":0,"output_tokens":0,"cache_read_input_tokens":3,"cache_creation_input_tokens":4}}}`, `{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}`, `{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}`, `{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"c1","name":"weather","input":{}}}`, `{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"city\":\"Paris\"}"}}`, `{"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":2}}`, `{"type":"message_stop"}`} {
		b, _ := json.Marshal(map[string]string{"bytes": base64.StdEncoding.EncodeToString([]byte(payload))})
		_ = frame(w, "chunk", string(b))
	}
}
func TestInvokeSyncAndStream(t *testing.T) {
	for _, streaming := range []bool{false, true} {
		t.Run(map[bool]string{false: "sync", true: "stream"}[streaming], func(t *testing.T) {
			p, e := provider(t)
			raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
				b, _ := io.ReadAll(r.Body)
				if string(b) != invokeBody {
					t.Error("request modified")
				}
				if streaming {
					w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
					invokeFrames(w)
				} else {
					w.Header().Set("Content-Type", "application/json")
					io.WriteString(w, invokeResult)
				}
			})
			c := NewClient(raw, WithTracerProvider(p))
			if streaming {
				out, err := c.InvokeModelWithResponseStream(context.Background(), &bedrockruntime.InvokeModelWithResponseStreamInput{ModelId: aws.String(model), Body: []byte(invokeBody), ContentType: aws.String("application/json")})
				if err != nil {
					t.Fatal(err)
				}
				n := 0
				for ev := range out.GetStream().Events() {
					v, ok := ev.(*types.ResponseStreamMemberChunk)
					if !ok || len(v.Value.Bytes) == 0 {
						t.Fatal("event lost")
					}
					n++
				}
				if n != 7 || out.GetStream().Err() != nil {
					t.Fatal(n, out.GetStream().Err())
				}
				_ = out.GetStream().Close()
			} else {
				out, err := c.InvokeModel(context.Background(), &bedrockruntime.InvokeModelInput{ModelId: aws.String(model), Body: []byte(invokeBody), ContentType: aws.String("application/json")})
				if err != nil || string(out.Body) != invokeResult {
					t.Fatal(err)
				}
			}
			spans := llms(e)
			if len(spans) != 1 {
				t.Fatal(len(spans))
			}
			a := attrs(spans[0])
			for k, v := range map[string]int64{"input_tokens": 0, "output_tokens": 2, "cache_read_tokens": 3, "cache_write_tokens": 4} {
				if a["gen_ai.usage."+k].Type() != attribute.INT64 || a["gen_ai.usage."+k].AsInt64() != v {
					t.Fatal(k, a)
				}
			}
			if !strings.Contains(a["output.value"].AsString(), "Paris") {
				t.Fatal(a)
			}
			data, _ := json.Marshal(spans)
			for _, bad := range []string{"binary-canary", "thinking-canary", "signature-canary"} {
				if strings.Contains(string(data), bad) {
					t.Fatal("unsupported content leaked")
				}
			}
		})
	}
}
func TestInvokeUnknownAndMissingUsage(t *testing.T) {
	p, e := provider(t)
	raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "stream") {
			w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
			invokeFrames(w)
		} else {
			w.Header().Set("Content-Type", "application/json")
			io.WriteString(w, `{"content":[{"type":"text","text":"answer"}]}`)
		}
	})
	c := NewClient(raw, WithTracerProvider(p))
	for _, m := range []string{"amazon.nova-test", "arn:aws:bedrock:us-east-1:0:provisioned-model/opaque"} {
		out, err := c.InvokeModel(context.Background(), &bedrockruntime.InvokeModelInput{ModelId: aws.String(m), Body: []byte(invokeBody)})
		if err != nil || out == nil {
			t.Fatal(err)
		}
	}
	out, err := c.InvokeModelWithResponseStream(context.Background(), &bedrockruntime.InvokeModelWithResponseStreamInput{ModelId: aws.String("amazon.nova-test"), Body: []byte(invokeBody)})
	if err != nil {
		t.Fatal(err)
	}
	for range out.GetStream().Events() {
	}
	_ = out.GetStream().Close()
	if len(llms(e)) != 0 {
		t.Fatal("fabricated unsupported spans")
	}
	_, err = c.InvokeModel(context.Background(), &bedrockruntime.InvokeModelInput{ModelId: aws.String(model), Body: []byte(invokeBody)})
	if err != nil {
		t.Fatal(err)
	}
	for k := range attrs(llms(e)[0]) {
		if strings.HasPrefix(k, "gen_ai.usage.") {
			t.Fatal("fabricated missing usage")
		}
	}
}
func TestInvokePrivacyStream(t *testing.T) {
	for _, config := range []TraceConfig{{HideInputs: true, HideOutputs: true}, {HideInputMessages: true, HideOutputMessages: true}, {HideInputText: true, HideOutputText: true}} {
		p, e := provider(t)
		raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
			invokeFrames(w)
		})
		c := NewClient(raw, WithTracerProvider(p), WithTraceConfig(config))
		out, err := c.InvokeModelWithResponseStream(context.Background(), &bedrockruntime.InvokeModelWithResponseStreamInput{ModelId: aws.String(model), Body: []byte(invokeBody)})
		if err != nil {
			t.Fatal(err)
		}
		for range out.GetStream().Events() {
		}
		_ = out.GetStream().Close()
		b, _ := json.Marshal(llms(e))
		for _, bad := range []string{"input-canary", "system-canary", "tool-canary", "Paris", "weather", "hello"} {
			if strings.Contains(string(b), bad) {
				t.Fatal("leaked " + bad)
			}
		}
	}
}
