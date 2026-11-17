package auto

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/baggage"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

// baggageSessionProcessor is an application span processor that copies a
// session.id baggage member onto every span it starts, as some applications
// do. The agent never wrote that value.
type baggageSessionProcessor struct{}

func (baggageSessionProcessor) OnStart(ctx context.Context, span sdktrace.ReadWriteSpan) {
	if id := baggage.FromContext(ctx).Member("session.id").Value(); id != "" {
		span.SetAttributes(attribute.String("session.id", id))
	}
}

func (baggageSessionProcessor) OnEnd(sdktrace.ReadOnlySpan) {}

func (baggageSessionProcessor) Shutdown(context.Context) error { return nil }

func (baggageSessionProcessor) ForceFlush(context.Context) error { return nil }

// exportedSessionID sends one AI request under ctx through the agent on an
// application-owned provider with the given span limits and processors. It
// returns the session.id of the wire span as Future AGI receives it: from the
// agent's own exporter when queued is true, else from a custom span processor.
func exportedSessionID(t *testing.T, ctx context.Context, queued bool, limits sdktrace.SpanLimits, processors ...sdktrace.SpanProcessor) (string, bool) {
	t.Helper()
	ownedRegressionSetup(t)
	received := make(chan *collector.ExportTraceServiceRequest, 8)
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		request := &collector.ExportTraceServiceRequest{}
		if err := proto.Unmarshal(data, request); err != nil {
			t.Error(err)
		}
		received <- request
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	t.Cleanup(sink.Close)
	t.Setenv("FI_BASE_URL", sink.URL)
	options := []sdktrace.TracerProviderOption{sdktrace.WithRawSpanLimits(limits)}
	for _, processor := range processors {
		options = append(options, sdktrace.WithSpanProcessor(processor))
	}
	p := sdktrace.NewTracerProvider(options...)
	t.Cleanup(func() { _ = p.Shutdown(context.Background()) })
	custom := tracetest.NewSpanRecorder()
	opts := []Option{WithTracerProvider(p), WithCredentials("SESSION_ORIGIN_FI_KEY", "SESSION_ORIGIN_FI_SECRET")}
	if !queued {
		opts = append(opts, WithSpanProcessor(custom))
	}
	if _, err := InitForTest(context.Background(), opts...); err != nil {
		t.Fatal(err)
	}
	r, _ := http.NewRequestWithContext(ctx, "POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
	resp, err := Transport(round5SuccessTransport()).RoundTrip(r)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if err := ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !queued {
		for _, span := range custom.Ended() {
			if spanAttr(span, "traceai.capture.layer").AsString() != "wire" {
				continue
			}
			value := spanAttr(span, "session.id")
			return value.AsString(), value.Type() == attribute.STRING
		}
		t.Fatal("the custom span processor received no wire span")
	}
	for {
		select {
		case export := <-received:
			for _, rs := range export.ResourceSpans {
				for _, ss := range rs.ScopeSpans {
					for _, span := range ss.Spans {
						attrs := map[string]string{}
						present := map[string]bool{}
						for _, kv := range span.Attributes {
							attrs[kv.Key] = kv.Value.GetStringValue()
							present[kv.Key] = true
						}
						if attrs["traceai.capture.layer"] == "wire" {
							return attrs["session.id"], present["session.id"]
						}
					}
				}
			}
		default:
			t.Fatal("the Future AGI exporter received no wire span")
			return "", false
		}
	}
}

// The session id set with ContextWithSessionID reaches Future AGI as its
// value through the agent's own exporter and through a custom span processor.
func TestContextSessionIDReachesEveryExportPath(t *testing.T) {
	for _, queued := range []bool{true, false} {
		t.Run(fmt.Sprintf("queued=%v", queued), func(t *testing.T) {
			ctx := ContextWithSessionID(context.Background(), "checkout-session-42")
			got, ok := exportedSessionID(t, ctx, queued, sdktrace.NewSpanLimits())
			if !ok || got != "checkout-session-42" {
				t.Fatalf("session.id = %q (present %v), want its value", got, ok)
			}
		})
	}
}

// Only the session id the agent wrote from ContextWithSessionID keeps its
// value, and only while the wire span still carries that exact value. An
// application span processor that sets session.id from baggage, alone or in
// place of the agent's value, gets the name rule: <redacted>.
func TestSessionIDFromAnotherProcessorIsRedacted(t *testing.T) {
	member, err := baggage.NewMember("session.id", "baggage-session-42")
	if err != nil {
		t.Fatal(err)
	}
	bag, err := baggage.New(member)
	if err != nil {
		t.Fatal(err)
	}
	for _, queued := range []bool{true, false} {
		for _, tc := range []struct {
			name string
			ctx  context.Context
		}{
			{"baggage-only", baggage.ContextWithBaggage(context.Background(), bag)},
			{"replaces-the-agent-value", baggage.ContextWithBaggage(ContextWithSessionID(context.Background(), "agent-session-7"), bag)},
		} {
			t.Run(fmt.Sprintf("queued=%v/%s", queued, tc.name), func(t *testing.T) {
				got, ok := exportedSessionID(t, tc.ctx, queued, sdktrace.NewSpanLimits(), baggageSessionProcessor{})
				if !ok || got != wire.CredentialRedacted {
					t.Fatalf("session.id = %q (present %v), want %q", got, ok, wire.CredentialRedacted)
				}
			})
		}
	}
}

// A value-length limit can cut the session id set with ContextWithSessionID
// in the middle of a known secret. The cut value takes the same fail-closed
// text path as any other cut attribute, so no part of the secret survives.
func TestCutSessionIDKeepsNoSecretFragment(t *testing.T) {
	const prefix = "checkout "
	const secret = "cut-session-secret-PLACEHOLDER"
	RegisterSecret(secret)
	limits := sdktrace.NewSpanLimits()
	// The cut keeps the first 11 characters of the secret: "cut-session".
	limits.AttributeValueLengthLimit = len(prefix) + 11
	for _, queued := range []bool{true, false} {
		t.Run(fmt.Sprintf("queued=%v", queued), func(t *testing.T) {
			ctx := ContextWithSessionID(context.Background(), prefix+secret)
			got, ok := exportedSessionID(t, ctx, queued, limits)
			if !ok || got == "" {
				t.Fatalf("session.id lost: %q (present %v)", got, ok)
			}
			for n := 4; n <= len(secret); n++ {
				if strings.Contains(got, secret[:n]) {
					t.Fatalf("session.id %q keeps %q of a known secret", got, secret[:n])
				}
			}
		})
	}
}
