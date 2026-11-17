package sdkcompat_test

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	anthropic "github.com/anthropics/anthropic-sdk-go"
	anthropicoption "github.com/anthropics/anthropic-sdk-go/option"
	"github.com/future-agi/traceAI/go/auto"
	"github.com/openai/openai-go/v3"
	openaioption "github.com/openai/openai-go/v3/option"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

const chatJSON = `{"id":"c1","model":"test-model","choices":[{"index":0,"message":{"role":"assistant","content":"Hello"},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}`
const messageJSON = `{"id":"msg_1","type":"message","role":"assistant","model":"test-model","content":[{"type":"text","text":"Hello"}],"stop_reason":"end_turn","usage":{"input_tokens":2,"output_tokens":1}}`

var startOnce sync.Once
var startProvider *sdktrace.TracerProvider
var startRecorder *tracetest.SpanRecorder
var startErr error

// start activates the agent once for this test binary. The package's own
// initialization stays inactive because the test environment has no Future AGI
// credentials, so this explicit Init is the first effective one.
func start(t *testing.T) (*sdktrace.TracerProvider, *tracetest.SpanRecorder) {
	t.Helper()
	startOnce.Do(func() {
		startProvider = sdktrace.NewTracerProvider()
		otel.SetTracerProvider(startProvider)
		startRecorder = tracetest.NewSpanRecorder()
		_, startErr = auto.Init(context.Background(), auto.WithSpanProcessor(startRecorder))
	})
	if startErr != nil {
		t.Fatal(startErr)
	}
	if report := auto.Report(); len(report.Integrations) == 0 || report.Integrations[0].Status != "instrumented" {
		t.Fatal("wire capture is not active: run without FI_API_KEY, FI_SECRET_KEY or a futureagi.toml so this test's Init takes effect")
	}
	return startProvider, startRecorder
}

func attr(span sdktrace.ReadOnlySpan, key string) attribute.Value {
	for _, kv := range span.Attributes() {
		if string(kv.Key) == key {
			return kv.Value
		}
	}
	return attribute.Value{}
}

func provider(t *testing.T, body string) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(server.Close)
	return server
}

// The default clients of both SDKs use http.DefaultTransport, so one blank
// import traces them without any client option.
func TestRealSDKDefaults(t *testing.T) {
	_, rec := start(t)
	for _, sdk := range []string{"openai", "anthropic"} {
		t.Run(sdk, func(t *testing.T) {
			before := len(rec.Ended())
			if sdk == "openai" {
				server := provider(t, chatJSON)
				client := openai.NewClient(openaioption.WithAPIKey("sdk-test"), openaioption.WithBaseURL(server.URL+"/v1/"), openaioption.WithMaxRetries(0))
				_, err := client.Chat.Completions.New(context.Background(), openai.ChatCompletionNewParams{Model: "test-model", Messages: []openai.ChatCompletionMessageParamUnion{openai.UserMessage("Hi")}})
				if err != nil {
					t.Fatal(err)
				}
			} else {
				server := provider(t, messageJSON)
				client := anthropic.NewClient(anthropicoption.WithAPIKey("sdk-test"), anthropicoption.WithBaseURL(server.URL), anthropicoption.WithMaxRetries(0))
				_, err := client.Messages.New(context.Background(), anthropic.MessageNewParams{Model: "test-model", MaxTokens: 16, Messages: []anthropic.MessageParam{anthropic.NewUserMessage(anthropic.NewTextBlock("Hi"))}})
				if err != nil {
					t.Fatal(err)
				}
			}
			var wire []sdktrace.ReadOnlySpan
			for _, span := range rec.Ended()[before:] {
				if attr(span, "traceai.capture.layer").AsString() == "wire" {
					wire = append(wire, span)
				}
			}
			if len(wire) != 1 || attr(wire[0], "output.value").AsString() != "Hello" {
				t.Fatalf("SDK default transport not covered: %d wire spans", len(wire))
			}
		})
	}
}

// A traceAI library span around a real SDK call suppresses the duplicate wire
// span, and the SDK's API key learned on the wire is redacted from that span.
func TestRealAnthropicDedupThroughNativeSpan(t *testing.T) {
	tp, rec := start(t)
	server := provider(t, messageJSON)
	before := len(rec.Ended())
	ctx, model := tp.Tracer("traceai-middleware").Start(context.Background(), "model", trace.WithAttributes(attribute.String("gen_ai.span.kind", "LLM")))
	client := anthropic.NewClient(anthropicoption.WithAPIKey("ANTHROPIC_PROVIDER_PRIVATE"), anthropicoption.WithBaseURL(server.URL), anthropicoption.WithMaxRetries(0))
	message, err := client.Messages.New(ctx, anthropic.MessageNewParams{Model: "test-model", MaxTokens: 16, Messages: []anthropic.MessageParam{anthropic.NewUserMessage(anthropic.NewTextBlock("hi"))}})
	if err != nil || message.ID != "msg_1" {
		t.Fatalf("SDK return value changed: %v", err)
	}
	model.SetAttributes(attribute.String("output.value", "ANTHROPIC_PROVIDER_PRIVATE"))
	model.End()
	spans := rec.Ended()[before:]
	if len(spans) == 0 {
		t.Fatal("library span was not recorded")
	}
	for _, span := range spans {
		if attr(span, "traceai.capture.layer").AsString() == "wire" {
			t.Fatal("real Anthropic library span broke wire deduplication")
		}
		if strings.Contains(attr(span, "output.value").AsString(), "ANTHROPIC_PROVIDER_PRIVATE") {
			t.Fatal("real SDK credential escaped library span privacy")
		}
	}
}
