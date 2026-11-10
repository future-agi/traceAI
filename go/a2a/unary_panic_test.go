package traceaia2a_test

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// Recovering a policy panic must not leave a client span waiting for a return
// that can never happen, regardless of where tracing sits in the policy chain.
func TestUnaryPolicyPanicCompletion(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, method := range []string{"SendMessage", "GetTask", "CancelTask"} {
			for _, first := range []bool{false, true} {
				for _, hidden := range []bool{false, true} {
					t.Run(fmt.Sprintf("%s/%s/first=%v/hidden=%v", transport, method, first, hidden), func(t *testing.T) {
						tp, rec := serverRecorder(t)
						// Even a protocol error used as a panic value is not a returned
						// agent error: its text must never become telemetry.
						panicValue := a2a.NewError(a2a.ErrUnauthorized, "private-policy-panic")
						policy := unaryPolicy{method: method, after: func(_ context.Context, resp *a2aclient.Response) error {
							if resp.Err != nil || resp.Payload == nil {
								t.Errorf("expected a successful response before panic: %+v", resp)
							}
							panic(panicValue)
						}}
						exec := endsIn(a2a.TaskStateInputRequired, "unaccepted-unary-output")
						exec.cancel = func(_ context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
							yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCanceled,
								a2a.NewMessageForTask(a2a.MessageRoleAgent, ec, a2a.NewTextPart("unaccepted-unary-output"))), nil)
						}
						client := qualificationClient(t, transport, exec, tp, traceaia2a.TraceConfig{HideInputs: hidden, HideOutputs: hidden}, first, policy)
						ctx, cancel := context.WithCancel(context.Background())
						defer cancel()
						var recovered any
						func() {
							defer func() { recovered = recover() }()
							_, _ = unaryCall(t, client, ctx, method)
						}()
						if recovered != panicValue {
							t.Fatalf("original panic identity changed: %v", recovered)
						}
						// No cancellation or wait should be required to finish a unary call.
						if len(rec.Started()) != len(rec.Ended()) {
							t.Errorf("unwinding leaked spans: started=%d ended=%d", len(rec.Started()), len(rec.Ended()))
						}
						cancel()
						count := 0
						for _, span := range rec.Ended() {
							if span.SpanKind() != trace.SpanKindClient || span.Name() != "a2a."+method {
								continue
							}
							count++
							a := attrs(span)
							if span.Status().Code != codes.Error || str(a, traceaia2a.ErrorType) != "panic" {
								t.Errorf("panic not classified safely: %v %v", span.Status(), a)
							}
							if has(a, traceaia2a.OutputValue) || has(a, traceaia2a.ErrorMessage) ||
								strings.Contains(everything(span), "private-policy-panic") || strings.Contains(everything(span), "unaccepted-unary-output") {
								t.Error("panic details or unaccepted output escaped")
							}
							if hidden && span.Status().Description != "" {
								t.Error("private status description escaped")
							}
						}
						if count != 1 {
							t.Fatalf("want exactly one completed client span, got %d", count)
						}
					})
				}
			}
		}
	}
}
