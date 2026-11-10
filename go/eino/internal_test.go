package traceaieino

import (
	"context"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/cloudwego/eino/callbacks"
	"github.com/cloudwego/eino/components"
	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/schema"
	"go.opentelemetry.io/otel/attribute"
)

func kvs(list []attribute.KeyValue) map[string]string {
	m := map[string]string{}
	for _, kv := range list {
		m[string(kv.Key)] = kv.Value.String()
	}
	return m
}

func strp(s string) *string { return &s }

// Multimodal messages: text and image URLs are kept, inline images become
// data: URLs that the TraceConfig redacts when long, audio/video/file bytes
// are never copied.
func TestMultimodalMessageAttributes(t *testing.T) {
	audio := strings.Repeat("QUFB", 100)
	msgs := []*schema.Message{{
		Role: schema.User,
		UserInputMultiContent: []schema.MessageInputPart{
			{Type: schema.ChatMessagePartTypeText, Text: "what is this?"},
			{Type: schema.ChatMessagePartTypeImageURL, Image: &schema.MessageInputImage{MessagePartCommon: schema.MessagePartCommon{URL: strp("https://img.example/cat.png")}}},
			{Type: schema.ChatMessagePartTypeImageURL, Image: &schema.MessageInputImage{MessagePartCommon: schema.MessagePartCommon{Base64Data: strp(strings.Repeat("A", 100)), MIMEType: "image/jpeg"}}},
			{Type: schema.ChatMessagePartTypeAudioURL, Audio: &schema.MessageInputAudio{MessagePartCommon: schema.MessagePartCommon{Base64Data: strp(audio)}}},
		},
	}, nil, {
		Role:                     schema.Assistant,
		AssistantGenMultiContent: []schema.MessageOutputPart{{Type: schema.ChatMessagePartTypeText, Text: "a cat"}},
	}}
	got := kvs(messageAttributes(GenAIInputMessages, msgs))
	p := GenAIInputMessages + ".0." + MessageContents + "."
	for k, want := range map[string]string{
		GenAIInputMessages + ".0." + MessageRole:                                  "user",
		p + "0." + MessageContentType:                                             "text",
		p + "0." + MessageContentText:                                             "what is this?",
		p + "1." + MessageContentImage:                                            "https://img.example/cat.png",
		p + "2." + MessageContentImage:                                            "data:image/jpeg;base64," + strings.Repeat("A", 100),
		p + "3." + MessageContentType:                                             "audio",
		GenAIInputMessages + ".1." + MessageContents + ".0." + MessageContentText: "a cat",
	} {
		if got[k] != want {
			t.Errorf("%s = %q, want %q", k, got[k], want)
		}
	}
	for k, v := range got {
		if strings.Contains(v, audio) {
			t.Fatalf("audio bytes copied into %s", k)
		}
	}
	// A long inline image is redacted by the config; a short one is kept.
	masked := kvs(TraceConfig{Base64ImageMaxLength: 50}.Filter(messageAttributes(GenAIInputMessages, msgs)))
	if masked[p+"2."+MessageContentImage] != RedactedValue {
		t.Fatalf("long inline image = %q", masked[p+"2."+MessageContentImage])
	}
	val := kvs(messagesValue(InputValue, InputMimeType, msgs))
	if !strings.Contains(val[InputValue], `"parts":["what is this?","[image]","[image]","[audio]"]`) {
		t.Fatalf("input.value = %s", val[InputValue])
	}
}

func TestToolResultPartsAndTruncate(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	info := &callbacks.RunInfo{Name: "search", Component: components.ComponentOfTool}
	ctx := h.OnStart(context.Background(), info, &tool.CallbackInput{ArgumentsInJSON: `{"q":"x"}`})
	h.OnEnd(ctx, info, &tool.CallbackOutput{ToolOutput: &schema.ToolResult{Parts: []schema.ToolOutputPart{
		{Type: schema.ToolPartTypeText, Text: "first"}, {Type: schema.ToolPartTypeText, Text: "second"}}}})
	if got, _ := attr(rec.Ended()[0], GenAIToolCallResult); got != "first\nsecond" {
		t.Fatalf("result = %q", got)
	}
	long := strings.Repeat("é", maxValueLen) // 2 bytes each
	cut := truncate(long, maxValueLen+1)
	if len(cut) > maxValueLen+1 || !utf8.ValidString(cut) {
		t.Fatalf("truncate split a rune: len=%d valid=%v", len(cut), utf8.ValidString(cut))
	}
	if truncate("short", 10) != "short" {
		t.Fatal("short strings are kept")
	}
}

// Streamed tool-call fragments are merged by index; text is joined; no usage
// is summed.
func TestJoinMessagesMergesToolCallFragments(t *testing.T) {
	i0, i1 := 0, 1
	m := joinMessages([]*schema.Message{
		{Role: schema.Assistant, ToolCalls: []schema.ToolCall{{Index: &i0, ID: "a", Function: schema.FunctionCall{Name: "f", Arguments: `{"x":`}}}},
		{ToolCalls: []schema.ToolCall{{Index: &i0, Function: schema.FunctionCall{Arguments: `1}`}}, {Index: &i1, ID: "b", Function: schema.FunctionCall{Name: "g", Arguments: `{}`}}}},
		{Content: "done", ResponseMeta: &schema.ResponseMeta{FinishReason: "tool_calls", Usage: &schema.TokenUsage{TotalTokens: 9}}},
		nil,
	})
	if m.Role != schema.Assistant || m.Content != "done" || len(m.ToolCalls) != 2 ||
		m.ToolCalls[0].Function.Arguments != `{"x":1}` || m.ToolCalls[1].ID != "b" || m.ResponseMeta.FinishReason != "tool_calls" {
		t.Fatalf("joined = %+v", m)
	}
	if m.ResponseMeta.Usage != nil {
		t.Fatal("join must not carry usage")
	}
	if joinMessages(nil) != nil {
		t.Fatal("no chunks, no message")
	}
}

// A streamed input to a chat model is recorded as its input messages.
func TestStreamedModelInput(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	info := &callbacks.RunInfo{Name: "chat", Component: components.ComponentOfChatModel}
	in, w := schema.Pipe[callbacks.CallbackInput](2)
	w.Send(&model.CallbackInput{Messages: []*schema.Message{schema.UserMessage("streamed question")}}, nil)
	w.Close()
	ctx := h.OnStartWithStreamInput(context.Background(), info, in)
	h.pending.Wait()
	h.OnEnd(ctx, info, &model.CallbackOutput{Message: schema.AssistantMessage("ok", nil)})
	got := kvs(rec.Ended()[0].Attributes())
	if got[GenAIInputMessages+".0."+MessageContent] != "streamed question" || got[OutputValue] != "ok" {
		t.Fatalf("attrs = %v", got)
	}
	// With inputs hidden the stream is closed unread.
	rec2, _, h2 := recorder(t, TraceConfig{HideInputs: true})
	in2, w2 := schema.Pipe[callbacks.CallbackInput](1)
	w2.Send(&model.CallbackInput{Messages: []*schema.Message{schema.UserMessage("SECRET")}}, nil)
	w2.Close()
	ctx2 := h2.OnStartWithStreamInput(context.Background(), info, in2)
	h2.OnEnd(ctx2, info, nil)
	for _, kv := range rec2.Ended()[0].Attributes() {
		if strings.Contains(kv.Value.String(), "SECRET") {
			t.Fatalf("%s leaked", kv.Key)
		}
	}
}

func TestContextIDs(t *testing.T) {
	ctx := context.Background()
	if ContextWithSessionID(ctx, "") != ctx || ContextWithUserID(ctx, "") != ctx {
		t.Fatal("an empty id must not change the context")
	}
	if len(ContextAttributes(ctx)) != 0 {
		t.Fatal("a bare context has no ids")
	}
	var nilCtx context.Context // a nil context, on purpose
	if _, ok := SessionIDFromContext(nilCtx); ok {
		t.Fatal("nil context")
	}
	if _, ok := UserIDFromContext(nilCtx); ok {
		t.Fatal("nil context")
	}
}

func TestSpanNameFallbacks(t *testing.T) {
	cases := map[string]*callbacks.RunInfo{
		"eino.run":        nil,
		"node":            {Name: "node", Type: "OpenAI", Component: components.ComponentOfChatModel},
		"OpenAIChatModel": {Type: "OpenAI", Component: components.ComponentOfChatModel},
		"MyChatModel":     {Type: "MyChatModel", Component: components.ComponentOfChatModel},
		"Retriever":       {Component: components.ComponentOfRetriever},
	}
	for want, info := range cases {
		if got := spanName(info); got != want {
			t.Errorf("spanName(%+v) = %q, want %q", info, got, want)
		}
	}
	if spanName(&callbacks.RunInfo{}) != "eino.run" {
		t.Error("empty RunInfo")
	}
}

// A panic inside a callback is contained.
func TestCallbacksNeverPanic(t *testing.T) {
	_, _, h := recorder(t, TraceConfig{})
	info := &callbacks.RunInfo{Name: "x", Component: components.ComponentOfChatModel}
	var nilCtx context.Context // a nil context, on purpose
	if got := h.OnStart(nilCtx, info, nil); got != nil {
		t.Fatal("nil context in, nil context out")
	}
	h.OnEnd(context.Background(), info, nil)
	h.OnError(context.Background(), info, nil)
	h.OnEndWithStreamOutput(context.Background(), info, nil)
	h.OnStartWithStreamInput(context.Background(), info, nil)
}
