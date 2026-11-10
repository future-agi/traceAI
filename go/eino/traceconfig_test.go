package traceaieino

import (
	"context"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func clearHideEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{EnvHideLLMInvocationParameters, EnvHideInputs, EnvHideOutputs, EnvHideInputMessages,
		EnvHideOutputMessages, EnvHideInputImages, EnvHideInputText, EnvHideOutputText, EnvBase64ImageMaxLength} {
		t.Setenv(k, "")
	}
}

func TestTraceConfigDefaultCapturesEverything(t *testing.T) {
	clearHideEnv(t)
	c := TraceConfigFromEnv()
	if c != (TraceConfig{}) {
		t.Fatalf("default must hide nothing, like DEFAULT_HIDE_* = False in Python: %+v", c)
	}
	for _, kv := range sampleAttributes() {
		got, keep := c.Mask(kv)
		if !keep || got != kv {
			t.Errorf("default config changed %s", kv.Key)
		}
	}
}

func TestTraceConfigFromEnvParsesLikePython(t *testing.T) {
	clearHideEnv(t)
	t.Setenv(EnvHideInputs, "TRUE")
	t.Setenv(EnvHideOutputs, "true")
	t.Setenv(EnvHideInputMessages, "1") // not "true": Python falls back to the default
	t.Setenv(EnvHideLLMInvocationParameters, "yes")
	t.Setenv(EnvBase64ImageMaxLength, "100")
	c := TraceConfigFromEnv()
	if !c.HideInputs || !c.HideOutputs || c.HideInputMessages || c.HideLLMInvocationParameters || c.Base64ImageMaxLength != 100 {
		t.Fatalf("parsed %+v", c)
	}
}

func sampleAttributes() []attribute.KeyValue {
	return []attribute.KeyValue{
		attribute.String(InputValue, "SECRET-in"),
		attribute.String(InputMimeType, MimeTypeJSON),
		attribute.String(OutputValue, "SECRET-out"),
		attribute.String(OutputMimeType, MimeTypeText),
		attribute.String(GenAIInputMessages+".0."+MessageRole, "user"),
		attribute.String(GenAIInputMessages+".0."+MessageContent, "SECRET-in-msg"),
		attribute.String(GenAIInputMessages+".1."+MessageContents+".0."+MessageContentText, "SECRET-in-part"),
		attribute.String(GenAIInputMessages+".1."+MessageContents+".1."+MessageContentImage, "https://img.example/x.png"),
		attribute.String(GenAIOutputMessages+".0."+MessageContent, "SECRET-out-msg"),
		attribute.String(GenAIOutputMessages+".0."+MessageToolCalls+".0."+ToolCallFunctionArguments, `{"q":"SECRET-args"}`),
		attribute.String(GenAIRequestParameters, `{"temperature":0}`),
		attribute.String(GenAIRetrievalQuery, "SECRET-query"),
		attribute.String(RetrievalDocuments+".0."+DocumentContent, "SECRET-doc"),
		attribute.String(RetrievalDocuments+".0."+DocumentID, "doc-1"),
		attribute.String(GenAIToolCallArguments, "SECRET-tool-args"),
		attribute.String(GenAIToolCallResult, "SECRET-tool-result"),
		attribute.String(ErrorMessage, "SECRET-error"),
		attribute.String(GenAIRequestModel, "gpt-x"),
		attribute.Int(GenAIUsageInputTokens, 3),
	}
}

func filtered(c TraceConfig) map[string]string {
	out := map[string]string{}
	for _, kv := range c.Filter(sampleAttributes()) {
		out[string(kv.Key)] = kv.Value.String()
	}
	return out
}

func TestHideInputsAndOutputsRemoveEveryContentValue(t *testing.T) {
	got := filtered(TraceConfig{HideInputs: true, HideOutputs: true})
	for k, v := range got {
		if strings.Contains(v, "SECRET") {
			t.Errorf("%s still carries content: %q", k, v)
		}
	}
	if got[InputValue] != RedactedValue || got[OutputValue] != RedactedValue {
		t.Errorf("input/output value must be the Python sentinel, got %q / %q", got[InputValue], got[OutputValue])
	}
	if _, ok := got[InputMimeType]; ok {
		t.Error("input.mime_type must be dropped")
	}
	if got[GenAIRequestModel] != "gpt-x" || got[GenAIUsageInputTokens] != "3" || got[RetrievalDocuments+".0."+DocumentID] != "doc-1" {
		t.Error("non-content attributes must survive")
	}
	if got[GenAIRequestParameters] == "" {
		t.Error("invocation parameters are controlled by their own switch")
	}
	for _, c := range []TraceConfig{{HideInputs: true}, {HideOutputs: true}} {
		if _, ok := filtered(c)[ErrorMessage]; ok {
			t.Errorf("%+v kept the API error message", c)
		}
		if c.errorMessageAllowed() {
			t.Errorf("%+v allows the error message in the status", c)
		}
	}
	if !(TraceConfig{}).errorMessageAllowed() || filtered(TraceConfig{})[ErrorMessage] == "" {
		t.Error("the default config keeps the API error message")
	}
}

func TestHideMessagesKeepsValue(t *testing.T) {
	got := filtered(TraceConfig{HideInputMessages: true, HideOutputMessages: true})
	for k := range got {
		if strings.Contains(k, GenAIInputMessages) || strings.Contains(k, GenAIOutputMessages) {
			t.Errorf("message attribute %s survived", k)
		}
	}
	if got[InputValue] != "SECRET-in" || got[OutputValue] != "SECRET-out" {
		t.Error("FI_HIDE_*_MESSAGES must leave input.value/output.value alone, as in Python")
	}
}

func TestHideTextRedactsMessageText(t *testing.T) {
	got := filtered(TraceConfig{HideInputText: true, HideOutputText: true})
	if got[GenAIInputMessages+".0."+MessageContent] != RedactedValue ||
		got[GenAIInputMessages+".1."+MessageContents+".0."+MessageContentText] != RedactedValue ||
		got[GenAIOutputMessages+".0."+MessageContent] != RedactedValue {
		t.Errorf("text not redacted: %v", got)
	}
	if got[GenAIInputMessages+".0."+MessageRole] != "user" {
		t.Error("role must survive")
	}
}

func TestImagesAndInvocationParameters(t *testing.T) {
	got := filtered(TraceConfig{HideInputImages: true, HideLLMInvocationParameters: true})
	if _, ok := got[GenAIInputMessages+".1."+MessageContents+".1."+MessageContentImage]; ok {
		t.Error("image survived FI_HIDE_INPUT_IMAGES")
	}
	if _, ok := got[GenAIRequestParameters]; ok {
		t.Error("parameters survived FI_HIDE_LLM_INVOCATION_PARAMETERS")
	}

	long := "data:image/png;base64," + strings.Repeat("A", 50)
	kv := attribute.String(GenAIInputMessages+".0."+MessageContents+".0."+MessageContentImage, long)
	m, keep := TraceConfig{Base64ImageMaxLength: 10}.Mask(kv)
	if !keep || m.Value.AsString() != RedactedValue {
		t.Errorf("long base64 image not redacted: %v", m.Value.AsString())
	}
	m, _ = TraceConfig{}.Mask(kv)
	if m.Value.AsString() != long {
		t.Error("short image under the default cap must be kept")
	}
}

func TestSetAttributesAppliesTheConfig(t *testing.T) {
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	_, span := tp.Tracer("t").Start(context.Background(), "tool")
	TraceConfig{HideInputs: true}.SetAttributes(span, attribute.String(InputValue, "SECRET"), attribute.String(GenAIToolName, "lookup"))
	span.End()
	attrs := map[string]string{}
	for _, kv := range rec.Ended()[0].Attributes() {
		attrs[string(kv.Key)] = kv.Value.String()
	}
	if attrs[InputValue] != RedactedValue || attrs[GenAIToolName] != "lookup" {
		t.Fatalf("attrs = %v", attrs)
	}
	TraceConfig{}.SetAttributes(nil) // must not panic
}
