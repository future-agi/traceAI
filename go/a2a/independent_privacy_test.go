package traceaia2a_test

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func TestIndependentNestedBeforeHonorsOuterPrivacy(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, innerPrivate := range []bool{false, true} {
			for _, tracingFirst := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/inner-private=%v/tracing-first=%v", transport, innerPrivate, tracingFirst), func(t *testing.T) {
					exp := tracetest.NewInMemoryExporter()
					tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
					defer tp.Shutdown(context.Background())
					innerInput, outerInput := "public inner input", "synthetic-private-outer-marker"
					privateMarker := outerInput
					if innerPrivate {
						innerInput, outerInput = "synthetic-private-inner-marker", "public outer input"
						privateMarker = innerInput
					}
					inner := qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{HideInputs: innerPrivate, HideOutputs: innerPrivate}, true)
					policy := unaryPolicy{method: "SendMessage", before: func(ctx context.Context, _ *a2aclient.Request) (context.Context, any, error) {
						_, err := inner.Client.SendMessage(ctx, userMessage(innerInput))
						return ctx, nil, err
					}}
					outer := qualificationClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{HideInputs: !innerPrivate, HideOutputs: !innerPrivate}, tracingFirst, policy)
					result, err := outer.SendMessage(context.Background(), userMessage(outerInput))
					if err != nil || result.(*a2a.Message).Parts[0].Text() != "hello "+outerInput {
						t.Fatalf("wrong application result: %v %v", result, err)
					}
					clients := 0
					for _, span := range exp.GetSpans() {
						for _, a := range span.Attributes {
							if strings.Contains(a.Value.Emit(), privateMarker) {
								t.Errorf("per-call HideInputs/HideOutputs bypassed: kind=%s input=%q attribute=%s value=%q", span.SpanKind, exportedAttr(span, traceaia2a.InputValue), a.Key, a.Value.Emit())
							}
						}
						if span.SpanKind != trace.SpanKindClient {
							continue
						}
						clients++
						input, output := exportedAttr(span, traceaia2a.InputValue), exportedAttr(span, traceaia2a.OutputValue)
						if input == traceaia2a.RedactedValue {
							if output != traceaia2a.RedactedValue {
								t.Error("private span output not redacted")
							}
						} else if output != "hello "+input {
							t.Error("public span received another call's output")
						}
					}
					if clients != 2 {
						t.Fatalf("want two independently completed clients, got %d", clients)
					}
				})
			}
		}
	}
}
