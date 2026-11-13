package auto

import (
	"context"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/protobuf/proto"
)

func TestS5HideFlattenedAttributeFamiliesOTLP(t *testing.T) {
	for _, tc := range []struct {
		name, root, operation string
		config                TraceConfig
	}{
		{"invocation", "gen_ai.request.parameters", "chat", TraceConfig{HideLLMInvocationParameters: true}},
		{"input", "gen_ai.request.parameters", "chat", TraceConfig{HideInputs: true}},
		{"input-text", "gen_ai.request.parameters", "chat", TraceConfig{HideInputText: true}},
		{"input-messages", "gen_ai.request.parameters", "chat", TraceConfig{HideInputMessages: true}},
		{"stt-output", "gen_ai.audio.transcript", "speech_to_text", TraceConfig{HideOutputs: true}},
		{"stt-output-text", "gen_ai.audio.transcript", "speech_to_text", TraceConfig{HideOutputText: true}},
		{"tts-input", "gen_ai.audio.transcript", "text_to_speech", TraceConfig{HideInputs: true}},
		{"tts-input-text", "gen_ai.audio.transcript", "text_to_speech", TraceConfig{HideInputText: true}},
		{"unknown-side-input", "gen_ai.audio.transcript", "", TraceConfig{HideInputs: true}},
		{"unknown-side-output", "gen_ai.audio.transcript", "", TraceConfig{HideOutputs: true}},
	} {
		for _, suffix := range []string{"", ".0", ".tools.0.description", ".prompt"} {
			t.Run(tc.name+"/"+suffix, func(t *testing.T) {
				exporterRegressionSetup(t)
				received := privacyCollector(t)
				const content = "PRIVATE_FLATTENED_CONTENT_MUST_BE_HIDDEN"
				key := tc.root + suffix
				attrs := []attribute.KeyValue{
					attribute.String(key, content),
					attribute.StringSlice(key+".1", []string{content}),
					attribute.String(tc.root+"_metadata", "SAFE_FAMILY_NEIGHBOR"),
					attribute.String("gen_ai.operation.name", tc.operation),
				}
				app := tracetest.NewSpanRecorder()
				p := sdktrace.NewTracerProvider(sdktrace.WithResource(resource.NewSchemaless(attrs...)), sdktrace.WithSpanProcessor(app))
				defer p.Shutdown(context.Background())
				if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("S5_HIDE_FI_KEY", "S5_HIDE_FI_SECRET"), WithTraceConfig(tc.config)); err != nil {
					t.Fatal(err)
				}
				parent := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{2}})
				_, span := p.Tracer("families", trace.WithInstrumentationAttributes(attrs...)).Start(context.Background(), "families", trace.WithAttributes(attrs...), trace.WithLinks(trace.Link{SpanContext: parent, Attributes: attrs}))
				span.AddEvent("families", trace.WithAttributes(attrs...))
				span.End()
				data, err := proto.Marshal(flushedPrivacyExport(t, received))
				if err != nil || strings.Contains(string(data), content) {
					t.Fatal("hidden family descendant reached collector")
				}
				if !strings.Contains(string(data), "SAFE_FAMILY_NEIGHBOR") {
					t.Fatal("family matching redacted a similarly named neighboring field")
				}
				if spanAttr(app.Ended()[0], key).AsString() != content {
					t.Fatal("hide controls mutated application exporter snapshot")
				}
			})
		}
	}
}

func TestS5TranscriptFamilyCorrectSideAndDefaultCapture(t *testing.T) {
	for _, tc := range []struct {
		operation string
		config    TraceConfig
	}{
		{"speech_to_text", TraceConfig{HideInputs: true}},
		{"text_to_speech", TraceConfig{HideOutputs: true}},
		{"speech_to_text", TraceConfig{}},
		{"text_to_speech", TraceConfig{}},
	} {
		for _, key := range []string{"gen_ai.audio.transcript", "gen_ai.audio.transcript.0", "gen_ai.audio.transcript.parts.0.text"} {
			kv := attribute.String(key, "visible transcript")
			got, keep := tc.config.mask(kv, tc.operation)
			if !keep || got.Value.AsString() != kv.Value.AsString() {
				t.Fatal("transcript hidden by wrong-side controls or default configuration")
			}
		}
	}
}
