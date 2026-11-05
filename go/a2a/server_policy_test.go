package traceaia2a_test

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
)

type responsePolicy struct {
	a2asrv.PassthroughCallInterceptor
	after func(context.Context, *a2asrv.CallContext, *a2asrv.Response) error
}

func (p responsePolicy) After(ctx context.Context, callCtx *a2asrv.CallContext, resp *a2asrv.Response) error {
	return p.after(ctx, callCtx, resp)
}

// After can reject a response by returning an error. The SDK then skips earlier
// After callbacks, so the wrapper must finish tracing without changing that order.
func TestServerResponsePolicy(t *testing.T) {
	for _, transport := range []string{"direct", "jsonrpc", "rest", "grpc"} {
		for _, config := range []struct {
			name string
			tc   traceaia2a.TraceConfig
		}{
			{"visible", traceaia2a.TraceConfig{}},
			{"hide-input", traceaia2a.TraceConfig{HideInputs: true}},
			{"hide-output", traceaia2a.TraceConfig{HideOutputs: true}},
		} {
			for _, test := range []struct {
				name      string
				streaming bool
				rejectAt  int32
			}{
				{"unary-rejection", false, 1},
				{"stream-rejection", true, 1},
				{"stream-late-rejection", true, 3},
				{"unary-allowed", false, 0},
				{"stream-allowed", true, 0},
			} {
				t.Run(transport+"/"+config.name+"/"+test.name, func(t *testing.T) {
					tp, rec := serverRecorder(t)
					tracing := traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(config.tc))
					rejection := a2a.NewError(a2a.ErrUnsupportedOperation, "output policy "+marker)
					var earlier, policy, later atomic.Int32
					count := func(n *atomic.Int32) a2asrv.CallInterceptor {
						return responsePolicy{after: func(context.Context, *a2asrv.CallContext, *a2asrv.Response) error {
							n.Add(1)
							return nil
						}}
					}
					outputPolicy := responsePolicy{after: func(_ context.Context, _ *a2asrv.CallContext, resp *a2asrv.Response) error {
						if resp.Payload == nil || resp.Err != nil {
							t.Error("policy did not receive the original successful response")
						}
						if policy.Add(1) == test.rejectAt {
							return rejection
						}
						return nil
					}}
					policies := a2asrv.WithCallInterceptors(tracing.Wrap(count(&earlier)), tracing.Wrap(outputPolicy), tracing.Wrap(count(&later)))
					exec := repliesWithMessage()
					if test.rejectAt > 1 {
						exec = completes()
					}
					handler := a2asrv.NewHandler(exec, a2asrv.WithCallInterceptors(tracing), policies)
					send, stream := handler.SendMessage, handler.SendStreamingMessage
					expected := 1
					if transport != "direct" {
						client := serverClient(t, transport, exec, tp, config.tc, policies)
						send, stream = client.SendMessage, client.SendStreamingMessage
						expected = 2
					}
					ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
					defer cancel()
					var got error
					if test.streaming {
						for _, err := range stream(ctx, userMessage("request "+marker)) {
							got = err
						}
					} else {
						_, got = send(ctx, userMessage("request "+marker))
					}
					if test.rejectAt == 0 {
						if got != nil {
							t.Fatal(got)
						}
					} else if !errors.Is(got, a2a.ErrUnsupportedOperation) || (transport == "direct" && got != rejection) {
						t.Fatalf("policy rejection changed: %v", got)
					}
					// Assert before canceling: cancellation must not mask a leaked span.
					if len(rec.Started()) != expected || len(rec.Ended()) != expected {
						t.Fatalf("completed call: started=%d ended=%d, want %d", len(rec.Started()), len(rec.Ended()), expected)
					}
					wantCalls, wantEarlier := int32(1), int32(1)
					if test.rejectAt > 0 {
						wantCalls, wantEarlier = test.rejectAt, test.rejectAt-1
					}
					if policy.Load() != wantCalls || later.Load() != wantCalls || earlier.Load() != wantEarlier {
						t.Fatalf("SDK callback order changed: earlier=%d policy=%d later=%d", earlier.Load(), policy.Load(), later.Load())
					}
					spans := serverSpans(rec)
					if len(spans) != 1 {
						t.Fatalf("want one server span, got %d", len(spans))
					}
					s := spans[0]
					a := attrs(s)
					if test.rejectAt > 0 {
						if s.Status().Code != codes.Error || str(a, traceaia2a.ErrorType) != "UNSUPPORTED_OPERATION" {
							t.Fatalf("rejection lost: status=%v attrs=%v", s.Status(), a)
						}
						if !config.tc.HideInputs && !config.tc.HideOutputs && !strings.Contains(str(a, traceaia2a.ErrorMessage), marker) {
							t.Fatal("visible protocol error text was lost")
						}
						if str(a, traceaia2a.OutputValue) != "" || (test.streaming && len(s.Events()) != int(test.rejectAt)-1) {
							t.Fatal("rejected output was captured as a successful response")
						}
					} else if s.Status().Code != codes.Ok {
						t.Fatalf("allowed response has status %v", s.Status())
					}
					if (config.tc.HideInputs || config.tc.HideOutputs) && str(a, traceaia2a.ErrorMessage) != "" {
						t.Fatal("hidden protocol error text leaked")
					}
				})
			}
		}
	}
}

type earlyResponsePolicy struct {
	a2asrv.PassthroughCallInterceptor
	result *a2a.Message
}

type policyContextKey struct{}

func (p earlyResponsePolicy) Before(ctx context.Context, _ *a2asrv.CallContext, req *a2asrv.Request) (context.Context, any, error) {
	req.Payload = p.result
	return context.WithValue(ctx, policyContextKey{}, p.result), p.result, a2a.ErrUnsupportedOperation
}

func TestServerWrapPreservesBefore(t *testing.T) {
	tp, rec := serverRecorder(t)
	tracing := traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp))
	result := a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("synthetic result"))
	wrapped := tracing.Wrap(earlyResponsePolicy{result: result})
	req := &a2asrv.Request{}
	ctx, got, err := wrapped.Before(context.Background(), nil, req)
	if got != result || err != a2a.ErrUnsupportedOperation || req.Payload != result || ctx.Value(policyContextKey{}) != result {
		t.Fatal("wrapped Before changed context, early response, error or request mutation")
	}
	resp := &a2asrv.Response{Payload: result}
	if err := wrapped.After(ctx, nil, resp); err != nil || resp.Payload != result || resp.Err != nil || len(rec.Started()) != 0 {
		t.Fatal("wrapped pass-through changed response or started an extra span")
	}
}
