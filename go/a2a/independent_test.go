package traceaia2a_test

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func exportedAttr(s tracetest.SpanStub, key string) string {
	for _, a := range s.Attributes {
		if string(a.Key) == key {
			return a.Value.AsString()
		}
	}
	return ""
}

// This smoke uses the real v2 client and loopback official servers, and checks
// what an actual in-memory exporter receives rather than only span processors.
func TestIndependentExporterSmoke(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, hide := range []bool{false, true} {
			for _, mode := range []string{"unary", "stream", "error"} {
				t.Run(fmt.Sprintf("%s/hidden=%v/%s", transport, hide, mode), func(t *testing.T) {
					exp := tracetest.NewInMemoryExporter()
					tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
					defer tp.Shutdown(context.Background())
					tc := traceaia2a.TraceConfig{HideInputs: hide, HideOutputs: hide}
					client := qualificationClient(t, transport, completes(), tp, tc, true)
					ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
					defer cancel()
					ctx, root := tp.Tracer("independent-smoke").Start(ctx, "root")
					method := "SendMessage"
					if mode == "stream" {
						method = "SendStreamingMessage"
						n := 0
						for _, err := range client.SendStreamingMessage(ctx, userMessage("independent input")) {
							if err != nil {
								t.Fatal(err)
							}
							n++
						}
						if n != 4 {
							t.Fatalf("wire event count=%d", n)
						}
					} else if mode == "error" {
						method = "GetTask"
						_, err := client.GetTask(ctx, &a2a.GetTaskRequest{ID: "synthetic-missing-task"})
						if err == nil {
							t.Fatal("expected protocol error")
						}
					} else {
						if _, err := client.SendMessage(ctx, userMessage("independent input")); err != nil {
							t.Fatal(err)
						}
					}
					root.End()
					if err := tp.ForceFlush(ctx); err != nil {
						t.Fatal(err)
					}
					spans := exp.GetSpans()
					if len(spans) != 3 {
						t.Fatalf("expected root/client/server only, got %d", len(spans))
					}
					var cs, ss *tracetest.SpanStub
					for i := range spans {
						s := &spans[i]
						if s.SpanKind == trace.SpanKindClient {
							cs = s
						}
						if s.SpanKind == trace.SpanKindServer {
							ss = s
						}
						if s.EndTime.Before(s.StartTime) || s.EndTime.IsZero() {
							t.Fatal("unended exported span")
						}
					}
					if cs == nil || ss == nil {
						t.Fatal("missing client/server")
					}
					if cs.Parent.SpanID() != root.SpanContext().SpanID() || ss.Parent.SpanID() != cs.SpanContext.SpanID() || !ss.Parent.IsRemote() {
						t.Fatal("broken remote tree")
					}
					for _, s := range []tracetest.SpanStub{*cs, *ss} {
						if s.Name != "a2a."+method || s.SpanContext.TraceID() != root.SpanContext().TraceID() || exportedAttr(s, traceaia2a.GenAISpanKind) != "AGENT" {
							t.Fatalf("wrong identity: %+v", s)
						}
						for _, a := range s.Attributes {
							k := string(a.Key)
							if strings.Contains(k, "gen_ai.usage") || strings.Contains(k, "token_count") || k == "gen_ai.provider.name" || k == "gen_ai.request.model" || strings.Contains(k, "cost") {
								t.Errorf("A2A fabricated model/provider/token/cost %v", a)
							}
						}
						if mode == "error" {
							if s.Status.Code != codes.Error || exportedAttr(s, traceaia2a.ErrorType) != "TASK_NOT_FOUND" {
								t.Errorf("wrong error %v %v", s.Status, s.Attributes)
							}
							if hide && (exportedAttr(s, traceaia2a.ErrorMessage) != "" || s.Status.Description != "") {
								t.Fatal("private error escaped")
							}
						} else {
							input, output := "independent input", "answer to independent input\ndone independent input"
							if hide {
								input = traceaia2a.RedactedValue
								output = input
							}
							if exportedAttr(s, traceaia2a.InputValue) != input || exportedAttr(s, traceaia2a.OutputValue) != output || s.Status.Code != codes.Ok {
								t.Errorf("wrong payload %v", s.Attributes)
							}
							if mode == "stream" && len(s.Events) != 4 {
								t.Errorf("event count=%d", len(s.Events))
							}
						}
					}
				})
			}
		}
	}
}

// A normal policy can make a nested SDK call in Before, before the outer tracing
// interceptor has started. Its successful result must not become the outer result.
func TestIndependentNestedBeforeCompletionOwnership(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			exp := tracetest.NewInMemoryExporter()
			tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
			defer tp.Shutdown(context.Background())
			inner := qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{}, true)
			policy := unaryPolicy{method: "SendMessage", before: func(ctx context.Context, _ *a2aclient.Request) (context.Context, any, error) {
				result, err := inner.Client.SendMessage(ctx, userMessage("inner input"))
				if err != nil {
					return ctx, nil, err
				}
				if result.(*a2a.Message).Parts[0].Text() != "hello inner input" {
					t.Fatal("wire nested result wrong")
				}
				return ctx, nil, nil
			}}
			outer := qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{}, false, policy)
			result, err := outer.SendMessage(context.Background(), userMessage("outer input"))
			if err != nil || result.(*a2a.Message).Parts[0].Text() != "hello outer input" {
				t.Fatalf("wire outer result wrong: %v %v", result, err)
			}
			count := 0
			for _, s := range exp.GetSpans() {
				if s.SpanKind != trace.SpanKindClient {
					continue
				}
				count++
				input := exportedAttr(s, traceaia2a.InputValue)
				output := exportedAttr(s, traceaia2a.OutputValue)
				if output != "hello "+input {
					t.Errorf("nested call outcome overwritten: input=%q output=%q", input, output)
				}
			}
			if count != 2 {
				t.Errorf("want two completed client spans, got %d", count)
			}
		})
	}
}
