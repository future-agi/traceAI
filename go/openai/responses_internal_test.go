package traceaiopenai

import (
	"context"
	"io"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func TestResponsePartialItemsAndMalformedEvents(t *testing.T) {
	a := newAccumulator()
	for _, data := range []string{
		`not json`, `null`, `{}`, `{"type":"unknown.future.event"}`,
		`{"type":"response.created","response":{"id":"resp_partial","model":"fixture"}}`,
		`{"type":"response.output_item.added","output_index":2,"item":{"type":"function_call","name":"lookup","call_id":"call_partial","arguments":""}}`,
		`{"type":"response.function_call_arguments.delta","output_index":2,"delta":"{\"q\":"}`,
		`{"type":"response.function_call_arguments.delta","output_index":2,"delta":"\"x\"}"}`,
		`{"type":"response.refusal.delta","output_index":0,"delta":"cannot "}`,
		`{"type":"response.refusal.delta","output_index":0,"delta":"answer"}`,
	} {
		if a.addResponseEvent([]byte(data)) {
			t.Fatal("nonterminal event ended response")
		}
	}
	got := map[string]attribute.Value{}
	for _, kv := range a.attributes(TraceConfig{}) {
		got[string(kv.Key)] = kv.Value
	}
	for key, want := range map[string]string{
		GenAIResponseID: "resp_partial", GenAIResponseModel: "fixture",
		"gen_ai.output.messages.0.message.content":                                   "cannot answer",
		"gen_ai.output.messages.1.message.tool_calls.0.tool_call.function.arguments": `{"q":"x"}`,
		"gen_ai.output.messages.1.message.tool_calls.0.tool_call.function.name":      "lookup",
		"gen_ai.output.messages.1.message.tool_calls.0.tool_call.id":                 "call_partial",
	} {
		if got[key].AsString() != want {
			t.Errorf("%s = %q, want %q", key, got[key].AsString(), want)
		}
	}
	if _, ok := got[GenAIUsageInputTokens]; ok {
		t.Error("invented usage")
	}
	for _, op := range []operation{opResponses, opEmbeddings} {
		for _, body := range []string{"", "null", "{", `{"model":5,"input":null}`, `{"input":[]}`} {
			p := parseRequest([]byte(body), TraceConfig{}, op)
			if p.model != "" {
				t.Error("malformed request invented model")
			}
			newAccumulator().addJSON([]byte(body), op)
		}
	}
}

func TestChatStreamErrorContractPreserved(t *testing.T) {
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	defer tp.Shutdown(context.Background())
	_, span := tp.Tracer("test").Start(context.Background(), "chat")
	body := newStreamBody(context.Background(), io.NopCloser(strings.NewReader("data: {\"error\":{\"code\":\"server_error\",\"message\":\"failed\"}}\n\ndata: [DONE]\n\n")), span, TraceConfig{}, opChat)
	_, _ = io.ReadAll(body)
	_ = body.Close()
	if len(rec.Ended()) != 1 {
		t.Fatal("expected one span")
	}
	s := rec.Ended()[0]
	if s.Status().Description != "error event in stream server_error: failed" {
		t.Errorf("chat status changed: %v", s.Status())
	}
	for _, kv := range s.Attributes() {
		if string(kv.Key) == ErrorType && kv.Value.AsString() != "stream_error" {
			t.Error("chat error class changed")
		}
	}
}
