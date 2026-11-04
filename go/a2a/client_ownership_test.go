package traceaia2a_test

import (
	"context"
	"fmt"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// Even the same SDK client, interceptor, card and payload are not a call identity.
// The outer boundary must bind before the first policy runs, and tracing must
// still find that binding when an earlier policy replaces the entire payload.
func TestNestedBeforeSameClientCompletion(t *testing.T) {
	type nestedKey struct{}
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, reject := range []bool{false, true} {
			for _, rewrite := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/reject=%v/rewrite=%v", transport, reject, rewrite), func(t *testing.T) {
					tp, rec := serverRecorder(t)
					var client *traceaia2a.Client
					rejection := a2a.NewError(a2a.ErrUnauthorized, "outer rejection")
					policy := unaryPolicy{method: "SendMessage", before: func(ctx context.Context, req *a2aclient.Request) (context.Context, any, error) {
						if ctx.Value(nestedKey{}) != nil {
							return ctx, nil, nil
						}
						result, err := client.Client.SendMessage(context.WithValue(ctx, nestedKey{}, true), req.Payload.(*a2a.SendMessageRequest))
						if err != nil || result.(*a2a.Message).Parts[0].Text() != "hello same payload" {
							t.Fatalf("nested application outcome changed: %v %v", result, err)
						}
						// The inner span must be complete before the outer tracing Before.
						clients := 0
						for _, span := range rec.Ended() {
							if span.SpanKind() == trace.SpanKindClient {
								clients++
								if str(attrs(span), traceaia2a.OutputValue) != "hello same payload" {
									t.Fatal("inner call lost its own result")
								}
							}
						}
						if clients != 1 {
							t.Fatalf("inner completion deferred to outer call: %d", clients)
						}
						if rewrite {
							req.Payload = userMessage("rewritten request")
						}
						return ctx, nil, nil
					}, after: func(ctx context.Context, resp *a2aclient.Response) error {
						if ctx.Value(nestedKey{}) != nil {
							return nil
						}
						if reject {
							return rejection
						}
						resp.Payload = a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("accepted outer response"))
						return nil
					}}
					client = qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{}, false, policy)
					result, err := client.SendMessage(context.Background(), userMessage("same payload"))
					if reject {
						if result != nil || err != rejection {
							t.Fatalf("outer rejection identity changed: %v %v", result, err)
						}
					} else if err != nil || result.(*a2a.Message).Parts[0].Text() != "accepted outer response" {
						t.Fatalf("outer accepted result changed: %v %v", result, err)
					}
					if len(rec.Started()) != len(rec.Ended()) {
						t.Fatal("nested invocation leaked a span")
					}
					clients := 0
					for _, span := range rec.Ended() {
						if span.SpanKind() != trace.SpanKindClient {
							continue
						}
						clients++
						if clients == 1 {
							if span.Status().Code != codes.Ok || str(attrs(span), traceaia2a.OutputValue) != "hello same payload" {
								t.Fatal("outer outcome corrupted completed inner span")
							}
						} else if reject {
							if span.Status().Code != codes.Error || has(attrs(span), traceaia2a.OutputValue) {
								t.Fatal("outer rejection lost or rejected output exported")
							}
						} else if span.Status().Code != codes.Ok || str(attrs(span), traceaia2a.OutputValue) != "accepted outer response" {
							t.Fatal("outer policy outcome not recorded")
						}
					}
					if clients != 2 {
						t.Fatalf("want two independently completed client spans, got %d", clients)
					}
				})
			}
		}
	}
}

// Source-compatible legacy wrapping must not inherit another client's new
// completion binding. Its unary behavior remains that of the bare interceptor.
func TestLegacyNestedUnaryCompletion(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, legacyInner := range []bool{false, true} {
			for _, legacyOuter := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/inner=%v/outer=%v", transport, legacyInner, legacyOuter), func(t *testing.T) {
					tp, rec := serverRecorder(t)
					inner := qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{}, true)
					if legacyInner {
						inner = traceaia2a.WrapClient(inner.Client)
					}
					policy := unaryPolicy{method: "SendMessage", before: func(ctx context.Context, _ *a2aclient.Request) (context.Context, any, error) {
						_, err := inner.SendMessage(ctx, userMessage("public inner"))
						return ctx, nil, err
					}}
					outer := qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{HideInputs: true, HideOutputs: true}, false, policy)
					if legacyOuter {
						outer = traceaia2a.WrapClient(outer.Client)
					}
					_, err := outer.SendMessage(context.Background(), userMessage("private outer"))
					if err != nil {
						t.Fatal(err)
					}
					clients := 0
					for _, span := range rec.Ended() {
						if span.SpanKind() != trace.SpanKindClient {
							continue
						}
						clients++
						a := attrs(span)
						switch str(a, traceaia2a.InputValue) {
						case "public inner":
							if str(a, traceaia2a.OutputValue) != "hello public inner" {
								t.Fatal("public inner span received the outer result")
							}
						case traceaia2a.RedactedValue:
							if str(a, traceaia2a.OutputValue) != traceaia2a.RedactedValue {
								t.Fatal("outer privacy lost")
							}
						default:
							t.Fatal("unexpected client input")
						}
					}
					if clients != 2 || len(rec.Started()) != len(rec.Ended()) {
						t.Fatal("legacy and bound clients did not complete independently")
					}
				})
			}
		}
	}
}
