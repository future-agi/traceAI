package traceaibedrock

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"go.opentelemetry.io/otel/attribute"
)

func TestCanonicalFirstTokenTiming(t *testing.T) {
	for _, operation := range []string{"ConverseStream", "InvokeModelWithResponseStream"} {
		t.Run(operation, func(t *testing.T) {
			p, e := provider(t)
			raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
				w.(http.Flusher).Flush()
				timer := time.NewTimer(50 * time.Millisecond)
				defer timer.Stop()
				select {
				case <-timer.C:
				case <-r.Context().Done():
					return
				}
				if operation == "ConverseStream" {
					converseFrames(w)
				} else {
					invokeFrames(w)
				}
			})
			started := time.Now()
			client := NewClient(raw, WithTracerProvider(p))
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if operation == "ConverseStream" {
				out, err := client.ConverseStream(ctx, &bedrockruntime.ConverseStreamInput{ModelId: aws.String(model), Messages: input().Messages})
				if err != nil {
					t.Fatal(err)
				}
				defer out.GetStream().Close()
				for range out.GetStream().Events() {
				}
				if err := out.GetStream().Err(); err != nil {
					t.Fatal(err)
				}
			} else {
				out, err := client.InvokeModelWithResponseStream(ctx, &bedrockruntime.InvokeModelWithResponseStreamInput{ModelId: aws.String(model), Body: []byte(invokeBody), ContentType: aws.String("application/json")})
				if err != nil {
					t.Fatal(err)
				}
				defer out.GetStream().Close()
				for range out.GetStream().Events() {
				}
				if err := out.GetStream().Err(); err != nil {
					t.Fatal(err)
				}
			}
			spans := llms(e)
			if len(spans) != 1 {
				t.Fatalf("want one span, got %d", len(spans))
			}
			a := attrs(spans[0])
			got := a["gen_ai.server.time_to_first_token"]
			// Future AGI's metric catalog, query and fi_types expect milliseconds.
			if got.Type() != attribute.FLOAT64 || got.AsFloat64() < 40 || got.AsFloat64() > float64(time.Since(started))/float64(time.Millisecond) {
				t.Errorf("Future AGI TTFT missing/wrong units: canonical type=%v value=%g ms, unrecognized latency key=%g seconds", got.Type(), got.AsFloat64(), a["gen_ai.latency.time_to_first_token"].AsFloat64())
			}
		})
	}
}
