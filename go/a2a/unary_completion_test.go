package traceaia2a_test

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

type unaryPolicy struct {
	a2aclient.PassthroughInterceptor
	method string
	before func(context.Context, *a2aclient.Request) (context.Context, any, error)
	after  func(context.Context, *a2aclient.Response) error
}

func (p unaryPolicy) Before(ctx context.Context, req *a2aclient.Request) (context.Context, any, error) {
	if req.Method == p.method && p.before != nil {
		return p.before(ctx, req)
	}
	return ctx, nil, nil
}

func (p unaryPolicy) After(ctx context.Context, resp *a2aclient.Response) error {
	if resp.Method == p.method && p.after != nil {
		return p.after(ctx, resp)
	}
	return nil
}

func unaryCall(t *testing.T, client *traceaia2a.Client, ctx context.Context, method string) (any, error) {
	t.Helper()
	if method == "SendMessage" {
		return client.SendMessage(ctx, userMessage("policy input"))
	}
	result, err := client.SendMessage(ctx, userMessage("seed task"))
	if err != nil {
		t.Fatal(err)
	}
	task := result.(*a2a.Task)
	if method == "GetTask" {
		return client.GetTask(ctx, &a2a.GetTaskRequest{ID: task.ID})
	}
	return client.CancelTask(ctx, &a2a.CancelTaskRequest{ID: task.ID})
}

func TestUnaryPolicyCompletion(t *testing.T) {
	hide, show := true, false
	configs := []struct {
		name   string
		tc     traceaia2a.TraceConfig
		hidden bool
	}{
		{"default", traceaia2a.TraceConfig{}, false},
		{"outputs", traceaia2a.TraceConfig{HideOutputs: true}, true},
		{"explicit", traceaia2a.TraceConfig{HideErrorDetails: &hide}, true},
		{"derived", traceaia2a.TraceConfig{HideInputMessages: true}, true},
		{"override", traceaia2a.TraceConfig{HideOutputs: true, HideErrorDetails: &show}, false},
	}
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, method := range []string{"SendMessage", "GetTask", "CancelTask"} {
			for _, tracingFirst := range []bool{true, false} {
				for _, config := range configs {
					t.Run(fmt.Sprintf("%s/%s/first=%v/%s", transport, method, tracingFirst, config.name), func(t *testing.T) {
						tp, rec := serverRecorder(t)
						rejection := a2a.NewError(a2a.ErrUnauthorized, "private-policy-error")
						var callbacks []string
						before := func(name string) func(context.Context, *a2aclient.Request) (context.Context, any, error) {
							return func(ctx context.Context, _ *a2aclient.Request) (context.Context, any, error) {
								callbacks = append(callbacks, "before-"+name)
								return ctx, nil, nil
							}
						}
						earlier := unaryPolicy{method: method, before: before("earlier"), after: func(context.Context, *a2aclient.Response) error {
							callbacks = append(callbacks, "after-earlier")
							return nil
						}}
						policy := unaryPolicy{method: method, before: before("policy"), after: func(_ context.Context, resp *a2aclient.Response) error {
							callbacks = append(callbacks, "after-policy")
							if resp.Err != nil || resp.Payload == nil {
								t.Errorf("fixture was not a successful response: %+v", resp)
							}
							return rejection
						}}
						client := qualificationClient(t, transport, endsIn(a2a.TaskStateInputRequired, "rejected-output"), tp, config.tc, tracingFirst, earlier, policy)
						ctx, cancel := context.WithCancel(context.Background())
						defer cancel()
						_, err := unaryCall(t, client, ctx, method)
						if err != rejection {
							t.Fatalf("error identity changed: %v", err)
						}
						if !slices.Equal(callbacks, []string{"before-earlier", "before-policy", "after-policy"}) {
							t.Fatalf("SDK callback ordering changed: %v", callbacks)
						}
						// Completion must not depend on cancellation, including Background callers.
						if len(rec.Started()) != len(rec.Ended()) {
							t.Fatalf("open spans before cancellation: started=%d ended=%d", len(rec.Started()), len(rec.Ended()))
						}
						cancel()
						count := 0
						for _, span := range rec.Ended() {
							if span.SpanKind() != trace.SpanKindClient || span.Name() != "a2a."+method {
								continue
							}
							count++
							a := attrs(span)
							if span.Status().Code != codes.Error || str(a, traceaia2a.ErrorType) != "UNAUTHORIZED" {
								t.Errorf("rejection not recorded: %v %v", span.Status(), a)
							}
							if has(a, traceaia2a.OutputValue) || strings.Contains(everything(span), "rejected-output") {
								t.Error("rejected output escaped into the client span")
							}
							if config.hidden {
								if has(a, traceaia2a.ErrorMessage) || span.Status().Description != "" {
									t.Error("private error details escaped")
								}
							} else if str(a, traceaia2a.ErrorMessage) != "private-policy-error" {
								t.Error("public error details missing")
							}
						}
						if count != 1 {
							t.Fatalf("want one completed client span, got %d", count)
						}
					})
				}
			}
		}
	}
}

func TestUnaryPolicyRewritesAcceptedOutput(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, method := range []string{"SendMessage", "GetTask", "CancelTask"} {
			for _, first := range []bool{true, false} {
				t.Run(fmt.Sprintf("%s/%s/first=%v", transport, method, first), func(t *testing.T) {
					tp, rec := serverRecorder(t)
					accepted := &a2a.Task{ID: "rewritten", Status: a2a.TaskStatus{State: a2a.TaskStateCompleted}, Artifacts: []*a2a.Artifact{{ID: "a", Parts: a2a.ContentParts{a2a.NewTextPart("accepted output")}}}}
					policy := unaryPolicy{method: method, after: func(_ context.Context, resp *a2aclient.Response) error { resp.Payload = accepted; return nil }}
					client := qualificationClient(t, transport, endsIn(a2a.TaskStateInputRequired, "obsolete output"), tp, traceaia2a.TraceConfig{}, first, policy)
					result, err := unaryCall(t, client, context.Background(), method)
					if err != nil || result != accepted {
						t.Fatalf("accepted result changed: %v, %v", result, err)
					}
					count := 0
					for _, span := range rec.Ended() {
						if span.SpanKind() != trace.SpanKindClient || span.Name() != "a2a."+method {
							continue
						}
						count++
						if span.Status().Code != codes.Ok || str(attrs(span), traceaia2a.OutputValue) != "accepted output" {
							t.Errorf("wrong accepted output: %v", attrs(span))
						}
					}
					if count != 1 || len(rec.Started()) != len(rec.Ended()) {
						t.Fatal("completion was not exactly once")
					}
				})
			}
		}
	}
}
