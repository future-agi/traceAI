package traceaibedrock

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
)

func TestLargeInvokeRequestRecognition(t *testing.T) {
	large := strings.Repeat("x", 1<<20)
	version := `"anthropic_version":"bedrock-2023-05-31"`
	for _, tc := range []struct {
		name, body string
		valid      bool
	}{
		{"text", largeInvokeRequest(1 << 20), true},
		{"metadata-after-content", `{"messages":[{"role":"user","content":"` + large + `"}],` + version + `}`, true},
		{"system", `{` + version + `,"messages":[],"system":"` + large + `"}`, true},
		{"tools", `{` + version + `,"messages":[],"tools":[{"name":"big","input_schema":{"description":"` + large + `"}}]}`, true},
		{"unknown-fields", `{"unknown":"` + large + `",` + version + `,"messages":[]}`, true},
		{"many-messages", `{` + version + `,"messages":[` + strings.Repeat(`{"role":"user","content":"hi"},`, 4096) + `{"role":"user","content":"last"}]}`, true},
		{"multimodal", `{` + version + `,"messages":[{"role":"user","content":[{"type":"image","source":{"type":"base64","data":"` + large + `"}}]}]}`, true},
		{"missing-messages", `{` + version + `,"system":"` + large + `"}`, false},
		{"null-messages", `{` + version + `,"messages":null,"system":"` + large + `"}`, false},
		{"string-messages", `{` + version + `,"messages":"` + large + `"}`, false},
		{"object-messages", `{` + version + `,"messages":{"text":"` + large + `"}}`, false},
		{"missing-version", `{"messages":[],"system":"` + large + `"}`, false},
		{"wrong-version", `{"anthropic_version":"unknown","messages":[],"system":"` + large + `"}`, false},
		{"huge-version", `{"anthropic_version":"` + large + `","messages":[]}`, false},
		{"nested-version", `{"unknown":{` + version + `},"messages":[],"system":"` + large + `"}`, false},
		{"malformed", largeInvokeRequest(1<<20) + `!`, false},
		{"duplicate-null-messages", `{` + version + `,"messages":[],"system":"` + large + `","messages":null}`, false},
		{"duplicate-null-version", `{` + version + `,"messages":[],"system":"` + large + `","anthropic_version":null}`, false},
		{"wrong-parameter-type", `{` + version + `,"messages":[],"system":"` + large + `","max_tokens":"bad"}`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if len(tc.body) <= 2*maxCapture {
				t.Fatal("fixture must exceed capture limit")
			}
			in := parseInvocation(aws.String(model), []byte(tc.body))
			if (in != nil) != tc.valid {
				t.Fatalf("recognized=%t, want %t", in != nil, tc.valid)
			}
			if in != nil && (len(in.Messages) != 0 || len(in.System) != 0 || len(in.Tools) != 0) {
				t.Fatal("oversized input content retained")
			}
		})
	}
}

func TestLargeInvokeRequestUntraced(t *testing.T) {
	for _, streaming := range []bool{false, true} {
		for _, mode := range []string{"suppressed", "unknown-model", "opaque-model", "unknown-schema"} {
			t.Run(fmt.Sprintf("stream=%t/%s", streaming, mode), func(t *testing.T) {
				p, e := provider(t)
				raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
					writeLargeInvokeResponse(t, w, streaming, true)
				})
				c := NewClient(raw, WithTracerProvider(p), WithTraceConfig(TraceConfig{}))
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				modelID, body := model, largeInvokeRequest(2*maxCapture+1)
				switch mode {
				case "suppressed":
					ctx = ContextWithSuppression(ctx)
				case "unknown-model":
					modelID = "amazon.nova-test"
				case "opaque-model":
					modelID = "arn:aws:bedrock:us-east-1:000000000000:provisioned-model/opaque"
				case "unknown-schema":
					body = strings.Replace(body, "bedrock-2023-05-31", "unknown", 1)
				}
				if err := callLargeInvoke(t, ctx, c, streaming, modelID, body, true); err != nil {
					t.Fatal(err)
				}
				if len(llms(e)) != 0 {
					t.Fatal("unsupported or suppressed call emitted a semantic span")
				}
			})
		}
	}
}

// The size limit bounds allocation as well as exported attributes. Large strings,
// arrays and media must not be copied merely to decide whether to start a span.
func BenchmarkParseLargeInvokeRequest(b *testing.B) {
	for _, size := range []int{70324, 1 << 20} {
		b.Run(fmt.Sprintf("bytes=%d", size), func(b *testing.B) {
			body := []byte(largeInvokeRequest(size))
			b.ReportAllocs()
			b.SetBytes(int64(size))
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if parseInvocation(aws.String(model), body) == nil {
					b.Fatal("valid request not recognized")
				}
			}
		})
	}
}
