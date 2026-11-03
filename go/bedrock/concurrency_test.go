package traceaibedrock

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/smithy-go/middleware"
	"github.com/aws/smithy-go/tracing/smithyoteltracing"
	smithyhttp "github.com/aws/smithy-go/transport/http"
	"github.com/aws/smithy-go/transport/http/protocol/restjson1"
	sdk "go.opentelemetry.io/otel/sdk/trace"
)

// The server barrier requires genuinely overlapping reads, not two sequential
// calls that happen to use goroutines. Two adapters also share one native client.
func TestConcurrentObservedStreams(t *testing.T) {
	for _, operations := range [][2]string{{"converse", "converse"}, {"invoke", "invoke"}, {"converse", "invoke"}} {
		for _, mode := range []string{"recording", "hidden", "suppressed", "unsampled", "unknown-model"} {
			t.Run(strings.Join(operations[:], "+")+"/"+mode, func(t *testing.T) {
				ready := make(chan struct{}, 2)
				release := make(chan struct{})
				p, exporter := provider(t)
				var hooks, calls atomic.Int32
				raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
					if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256") {
						t.Error("signing lost")
					}
					calls.Add(1)
					w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
					w.(http.Flusher).Flush()
					ready <- struct{}{}
					select {
					case <-release:
					case <-r.Context().Done():
						return
					}
					if strings.HasSuffix(r.URL.Path, "converse-stream") {
						converseFrames(w)
					} else {
						invokeFrames(w)
					}
				}, func(o *bedrockruntime.Options) {
					o.TracerProvider = smithyoteltracing.Adapt(p)
					o.APIOptions = append(o.APIOptions, func(*middleware.Stack) error { hooks.Add(1); return nil })
				})
				originalProtocol := raw.Options().Protocol
				config := []Option{WithTracerProvider(p), WithTraceConfig(TraceConfig{HideOutputs: mode == "hidden"})}
				if mode == "unsampled" {
					dropped := sdk.NewTracerProvider(sdk.WithSampler(sdk.NeverSample()))
					defer dropped.Shutdown(context.Background())
					config = append(config, WithTracerProvider(dropped))
				}
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				if mode == "suppressed" {
					ctx = ContextWithSuppression(ctx)
				}
				var wg sync.WaitGroup
				wantSpans := 0
				for _, op := range operations {
					if mode != "suppressed" && mode != "unsampled" && !(mode == "unknown-model" && op == "invoke") {
						wantSpans++
					}
					wg.Go(func() {
						client := NewClient(raw, config...)
						// Even caller overrides using the shared standard protocol
						// must be isolated; the original client must remain unchanged.
						option := func(o *bedrockruntime.Options) { o.Protocol = originalProtocol }
						if op == "converse" {
							out, err := client.ConverseStream(ctx, &bedrockruntime.ConverseStreamInput{ModelId: aws.String(model), Messages: input().Messages}, option)
							if err != nil {
								t.Error(err)
								return
							}
							checkStreamEvents(t, out.GetStream(), 8)
						} else {
							id := model
							if mode == "unknown-model" {
								id = "amazon.nova-test"
							}
							out, err := client.InvokeModelWithResponseStream(ctx, &bedrockruntime.InvokeModelWithResponseStreamInput{ModelId: &id, Body: []byte(invokeBody)}, option)
							if err != nil {
								t.Error(err)
								return
							}
							checkStreamEvents(t, out.GetStream(), 7)
						}
					})
				}
				for i := 0; i < 2; i++ {
					select {
					case <-ready:
					case <-ctx.Done():
						t.Error("requests did not overlap")
					}
				}
				close(release)
				wg.Wait()
				if got := len(llms(exporter)); got != wantSpans {
					t.Errorf("LLM spans = %d, want %d", got, wantSpans)
				}
				if hooks.Load() != 2 || calls.Load() != 2 {
					t.Error("native options lost")
				}
				// Native tracing correctly inherits the unsampled semantic parent.
				if mode != "unsampled" && len(exporter.GetSpans()) <= wantSpans {
					t.Error("native tracing lost")
				}
				if raw.Options().Protocol != originalProtocol {
					t.Error("mutated native client")
				}
			})
		}
	}
}

func checkStreamEvents[T any](t *testing.T, s reader[T], want int) {
	t.Helper()
	defer s.Close()
	count := 0
	for range s.Events() {
		count++
	}
	if count != want || s.Err() != nil {
		t.Errorf("events=%d want=%d err=%v", count, want, s.Err())
	}
}

// Custom protocol implementations are not silently replaced with restJson1.
// As with custom transports/runtimes, their concurrency contract is caller-owned.
type customProtocol struct{ smithyhttp.ClientProtocol }

func TestStreamProtocolOptions(t *testing.T) {
	custom := &customProtocol{ClientProtocol: restjson1.New(nil)}
	for _, protocol := range []smithyhttp.ClientProtocol{restjson1.New(nil), custom, nil} {
		var calls int
		sentinel := func(*bedrockruntime.Options) { calls += 100 }
		backing := []func(*bedrockruntime.Options){func(o *bedrockruntime.Options) { calls++; o.Protocol = protocol }, sentinel}
		options := streamOptions(backing[:1])
		a, b := bedrockruntime.Options{}, bedrockruntime.Options{}
		for _, option := range options {
			option(&a)
			option(&b)
		}
		backing[1](&a)
		if calls != 102 {
			t.Fatal("changed options or caller backing array")
		}
		if _, standard := protocol.(*restjson1.Protocol); standard {
			if a.Protocol == protocol || a.Protocol == b.Protocol {
				t.Fatal("shared mutable protocol")
			}
		} else if a.Protocol != protocol || b.Protocol != protocol {
			t.Fatal("custom protocol changed")
		}
	}
}

func TestStreamOptionsErrorAndRetryFidelity(t *testing.T) {
	for _, operation := range []string{"converse", "invoke"} {
		for _, setup := range []bool{false, true} {
			t.Run(operation+map[bool]string{true: "/setup-error", false: "/retry"}[setup], func(t *testing.T) {
				p, e := provider(t)
				var calls atomic.Int32
				raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
					if calls.Add(1) == 1 {
						w.Header().Set("X-Amzn-ErrorType", "ThrottlingException")
						w.WriteHeader(429)
						return
					}
					w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
					if operation == "converse" {
						converseFrames(w)
					} else {
						invokeFrames(w)
					}
				})
				client := NewClient(raw, WithTracerProvider(p))
				sentinel := errors.New("private setup error")
				option := func(o *bedrockruntime.Options) {
					if setup {
						o.APIOptions = append(o.APIOptions, func(*middleware.Stack) error { return sentinel })
					}
				}
				var err error
				if operation == "converse" {
					var out *ConverseStreamOutput
					out, err = client.ConverseStream(context.Background(), &bedrockruntime.ConverseStreamInput{ModelId: aws.String(model), Messages: input().Messages}, option)
					if err == nil {
						checkStreamEvents(t, out.GetStream(), 8)
					}
				} else {
					var out *InvokeModelWithResponseStreamOutput
					out, err = client.InvokeModelWithResponseStream(context.Background(), &bedrockruntime.InvokeModelWithResponseStreamInput{ModelId: aws.String(model), Body: []byte(invokeBody)}, option)
					if err == nil {
						checkStreamEvents(t, out.GetStream(), 7)
					}
				}
				if setup {
					if !errors.Is(err, sentinel) || calls.Load() != 0 {
						t.Fatal("setup error fidelity", err)
					}
				} else if err != nil || calls.Load() != 2 {
					t.Fatal("retry fidelity", err, calls.Load())
				}
				if len(llms(e)) != 1 {
					t.Fatal("logical span count")
				}
			})
		}
	}
}
