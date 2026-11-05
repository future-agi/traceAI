package traceaia2a_test

import (
	"context"
	"fmt"
	"sync"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	a2agrpc "github.com/a2aproject/a2a-go/v2/a2agrpc/v1"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func TestEndpointClientCompletion(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			server := qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{HideInputs: true, HideOutputs: true}, true)
			newClient := func(tc traceaia2a.TraceConfig, policies ...a2aclient.CallInterceptor) *traceaia2a.Client {
				t.Helper()
				policies = append(policies, traceaia2a.NewInterceptor(traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(tc)))
				opts := []a2aclient.FactoryOption{
					a2aclient.WithCallInterceptors(policies...),
					a2aclient.WithConfig(a2aclient.Config{AcceptedOutputModes: []string{"text/plain"}}),
				}
				if transport == "grpc" {
					opts = append(opts, a2agrpc.WithGRPCTransport(grpc.WithTransportCredentials(insecure.NewCredentials())))
				}
				client, err := traceaia2a.NewClientFromEndpoints(context.Background(), server.Card().SupportedInterfaces, opts...)
				if err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { _ = client.Destroy() })
				if client.Card() != nil {
					t.Fatal("endpoint fixture unexpectedly has a card")
				}
				return client
			}
			inner := newClient(traceaia2a.TraceConfig{})
			policy := unaryPolicy{method: "SendMessage", before: func(ctx context.Context, req *a2aclient.Request) (context.Context, any, error) {
				_, err := inner.Client.SendMessage(ctx, userMessage("public endpoint call"))
				req.Payload = userMessage("private rewritten input")
				return ctx, nil, err
			}}
			outer := newClient(traceaia2a.TraceConfig{HideInputs: true, HideOutputs: true}, policy)
			result, err := outer.SendMessage(context.Background(), userMessage("private input"))
			if err != nil || result.(*a2a.Message).Parts[0].Text() != "hello private rewritten input" {
				t.Fatalf("endpoint outcome changed: %v %v", result, err)
			}
			clients := 0
			for _, span := range rec.Ended() {
				if span.SpanKind() != trace.SpanKindClient {
					continue
				}
				clients++
				a := attrs(span)
				if clients == 1 {
					if str(a, traceaia2a.InputValue) != "public endpoint call" || str(a, traceaia2a.OutputValue) != "hello public endpoint call" {
						t.Fatal("nil-card clients shared completion")
					}
				} else if str(a, traceaia2a.InputValue) != traceaia2a.RedactedValue || str(a, traceaia2a.OutputValue) != traceaia2a.RedactedValue {
					t.Fatal("endpoint privacy lost after payload replacement")
				}
			}
			if clients != 2 || len(rec.Started()) != len(rec.Ended()) {
				t.Fatal("endpoint clients did not complete independently")
			}
		})
	}
}

func TestConcurrentClientCompletionWithSharedMessage(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			client := qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{}, true)
			request := userMessage("shared request")
			const calls = 12
			var wg sync.WaitGroup
			for index := range calls {
				wg.Go(func() {
					ctx := traceaia2a.ContextWithSessionID(context.Background(), fmt.Sprint(index))
					// The SDK writes req.Tenant, so concurrent requests need their own
					// envelope even when they share an immutable message and client.
					req := *request
					result, err := client.SendMessage(ctx, &req)
					if err != nil {
						t.Error(err)
						return
					}
					if result.(*a2a.Message).Parts[0].Text() != "hello shared request" {
						t.Error("shared request result changed")
					}
				})
			}
			wg.Wait()
			seen := make(map[string]int)
			for _, span := range rec.Ended() {
				if span.SpanKind() != trace.SpanKindClient {
					continue
				}
				a := attrs(span)
				seen[str(a, traceaia2a.SessionID)]++
				if span.Status().Code != codes.Ok || str(a, traceaia2a.OutputValue) != "hello shared request" {
					t.Fatal("shared request completion changed")
				}
			}
			if len(seen) != calls || len(rec.Started()) != len(rec.Ended()) {
				t.Fatal("concurrent completions were lost")
			}
			for id, count := range seen {
				if count != 1 {
					t.Errorf("session %s has %d completed spans", id, count)
				}
			}
		})
	}
}

func TestNestedBeforeOuterShortCircuit(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, reject := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/reject=%v", transport, reject), func(t *testing.T) {
				tp, rec := serverRecorder(t)
				inner := qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{}, true)
				rejection := a2a.NewError(a2a.ErrUnauthorized, "private early rejection")
				accepted := a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("private early result"))
				policy := unaryPolicy{method: "SendMessage", before: func(ctx context.Context, _ *a2aclient.Request) (context.Context, any, error) {
					_, err := inner.Client.SendMessage(ctx, userMessage("inner input"))
					if err != nil {
						return ctx, nil, err
					}
					if reject {
						return ctx, nil, rejection
					}
					return ctx, accepted, nil
				}}
				outer := qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{HideInputs: true, HideOutputs: true}, false, policy)
				result, err := outer.SendMessage(context.Background(), userMessage("private input"))
				if (reject && (result != nil || err != rejection)) || (!reject && (result != accepted || err != nil)) {
					t.Fatalf("short-circuit result changed: %v %v", result, err)
				}
				clients := 0
				for _, span := range rec.Ended() {
					if span.SpanKind() != trace.SpanKindClient {
						continue
					}
					clients++
					if span.Status().Code != codes.Ok || str(attrs(span), traceaia2a.OutputValue) != "hello inner input" {
						t.Fatal("untraced outer result was recorded in inner span")
					}
				}
				if clients != 1 || len(rec.Started()) != len(rec.Ended()) {
					t.Fatal("short-circuit invented or leaked a span")
				}
			})
		}
	}
}
