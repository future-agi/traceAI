package traceaieino

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/cloudwego/eino/adk"
	"github.com/cloudwego/eino/callbacks"
	"github.com/cloudwego/eino/components"
	"github.com/cloudwego/eino/components/embedding"
	"github.com/cloudwego/eino/components/indexer"
	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/components/prompt"
	"github.com/cloudwego/eino/components/retriever"
	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/compose"
	"github.com/cloudwego/eino/schema"
	"go.opentelemetry.io/otel/attribute"
)

// maxValueLen caps input.value, output.value and every other content string.
const maxValueLen = 32_000

// spanKind maps RunInfo.Component to gen_ai.span.kind.
func spanKind(info *callbacks.RunInfo) string {
	if info == nil {
		return SpanKindUnknown
	}
	switch info.Component {
	case components.ComponentOfChatModel, components.ComponentOfAgenticModel:
		return SpanKindLLM
	case components.ComponentOfTool:
		return SpanKindTool
	case components.ComponentOfRetriever:
		return SpanKindRetriever
	case components.ComponentOfEmbedding:
		return SpanKindEmbedding
	case adk.ComponentOfAgent, adk.ComponentOfAgenticAgent:
		return SpanKindAgent
	case compose.ComponentOfGraph, compose.ComponentOfChain, compose.ComponentOfWorkflow,
		compose.ComponentOfLambda, compose.ComponentOfPassthrough, compose.ComponentOfToolsNode,
		components.ComponentOfPrompt, components.ComponentOfAgenticPrompt, components.ComponentOfIndexer,
		components.ComponentOfLoader, components.ComponentOfTransformer:
		return SpanKindChain
	}
	return SpanKindUnknown
}

func operationName(kind string) string {
	switch kind {
	case SpanKindLLM:
		return "chat"
	case SpanKindTool:
		return "execute_tool"
	case SpanKindEmbedding:
		return "embeddings"
	case SpanKindAgent:
		return "invoke_agent"
	}
	return ""
}

// spanName is the run's name as Eino shows it: the node, tool or agent name
// when one is set, otherwise "{Type}{Component}" (for example
// OpenAIChatModel), otherwise the component.
func spanName(info *callbacks.RunInfo) string {
	if info == nil {
		return "eino.run"
	}
	if info.Name != "" {
		return info.Name
	}
	if info.Type != "" && !strings.HasSuffix(info.Type, string(info.Component)) {
		return info.Type + string(info.Component)
	}
	if info.Type != "" {
		return info.Type
	}
	if info.Component != "" {
		return string(info.Component)
	}
	return "eino.run"
}

// identityAttributes are written on every span, in every privacy mode.
func identityAttributes(info *callbacks.RunInfo, kind string) []attribute.KeyValue {
	kvs := []attribute.KeyValue{attribute.String(GenAISpanKind, kind)}
	if op := operationName(kind); op != "" {
		kvs = append(kvs, attribute.String(GenAIOperationName, op))
	}
	if info == nil {
		return kvs
	}
	if info.Component != "" {
		kvs = append(kvs, attribute.String(EinoComponent, string(info.Component)))
	}
	if info.Type != "" {
		kvs = append(kvs, attribute.String(EinoType, info.Type))
	}
	if info.Name != "" {
		kvs = append(kvs, attribute.String(EinoName, info.Name))
		switch kind {
		case SpanKindTool:
			kvs = append(kvs, attribute.String(GenAIToolName, info.Name))
		case SpanKindAgent:
			kvs = append(kvs, attribute.String(GenAIAgentName, info.Name))
		}
	}
	return kvs
}

// inputAttributes maps a callback input to span attributes. Content keys are
// filtered by the TraceConfig in start.
func (h *Handler) inputAttributes(ctx context.Context, info *callbacks.RunInfo, input callbacks.CallbackInput) []attribute.KeyValue {
	if input == nil {
		return nil
	}
	switch spanKind(info) {
	case SpanKindLLM:
		if mi := model.ConvCallbackInput(input); mi != nil {
			return h.modelInputAttributes(mi)
		}
	case SpanKindTool:
		kvs := []attribute.KeyValue{}
		if id := compose.GetToolCallID(ctx); id != "" {
			kvs = append(kvs, attribute.String(GenAIToolCallID, id))
		}
		if ti := tool.ConvCallbackInput(input); ti != nil {
			args := truncate(ti.ArgumentsInJSON, maxValueLen)
			return append(kvs,
				attribute.String(GenAIToolCallArguments, args),
				attribute.String(InputValue, args),
				attribute.String(InputMimeType, MimeTypeJSON))
		}
		return append(kvs, valueAttributes(InputValue, InputMimeType, input)...)
	case SpanKindRetriever:
		if ri := retriever.ConvCallbackInput(input); ri != nil {
			kvs := []attribute.KeyValue{
				attribute.String(GenAIRetrievalQuery, truncate(ri.Query, maxValueLen)),
				attribute.String(InputValue, truncate(ri.Query, maxValueLen)),
				attribute.String(InputMimeType, MimeTypeText),
			}
			if ri.TopK > 0 {
				kvs = append(kvs, attribute.Int(GenAIRetrievalTopK, ri.TopK))
			}
			return kvs
		}
	case SpanKindEmbedding:
		if ei := embedding.ConvCallbackInput(input); ei != nil {
			var kvs []attribute.KeyValue
			if ei.Config != nil && ei.Config.Model != "" {
				kvs = append(kvs, attribute.String(GenAIRequestModel, ei.Config.Model))
			}
			return append(kvs, valueAttributes(InputValue, InputMimeType, ei.Texts)...)
		}
	case SpanKindAgent:
		if ai := adk.ConvAgentCallbackInput(input); ai != nil {
			var kvs []attribute.KeyValue
			if ai.ResumeInfo != nil {
				kvs = append(kvs, attribute.Bool(EinoAgentResumed, true))
			}
			if ai.Input != nil {
				kvs = append(kvs, messagesValue(InputValue, InputMimeType, ai.Input.Messages)...)
			}
			return kvs
		}
		if ai := adk.ConvTypedCallbackInput[*schema.AgenticMessage](input); ai != nil {
			var kvs []attribute.KeyValue
			if ai.ResumeInfo != nil {
				kvs = append(kvs, attribute.Bool(EinoAgentResumed, true))
			}
			if ai.Input != nil {
				kvs = append(kvs, valueAttributes(InputValue, InputMimeType, ai.Input.Messages)...)
			}
			return kvs
		}
	}
	if info != nil {
		switch info.Component {
		case components.ComponentOfPrompt:
			if pi := prompt.ConvCallbackInput(input); pi != nil {
				return valueAttributes(InputValue, InputMimeType, pi.Variables)
			}
		case components.ComponentOfIndexer:
			if ii := indexer.ConvCallbackInput(input); ii != nil {
				return valueAttributes(InputValue, InputMimeType, ii.Docs)
			}
		}
	}
	return valueAttributes(InputValue, InputMimeType, input)
}

func (h *Handler) modelInputAttributes(mi *model.CallbackInput) []attribute.KeyValue {
	var kvs []attribute.KeyValue
	params := map[string]any{}
	if c := mi.Config; c != nil {
		if c.Model != "" {
			kvs = append(kvs, attribute.String(GenAIRequestModel, c.Model))
		}
		// Eino's Config holds plain numbers, so an unset value and 0 look
		// the same. Only non-zero values are written; 0 is never invented.
		if c.MaxTokens > 0 {
			kvs = append(kvs, attribute.Int(GenAIRequestMaxTokens, c.MaxTokens))
			params["max_tokens"] = c.MaxTokens
		}
		if c.Temperature != 0 {
			kvs = append(kvs, attribute.Float64(GenAIRequestTemperature, roundFloat(c.Temperature)))
			params["temperature"] = roundFloat(c.Temperature)
		}
		if c.TopP != 0 {
			kvs = append(kvs, attribute.Float64(GenAIRequestTopP, roundFloat(c.TopP)))
			params["top_p"] = roundFloat(c.TopP)
		}
		if len(c.Stop) > 0 {
			kvs = append(kvs, attribute.StringSlice(GenAIRequestStopSequences, c.Stop))
			params["stop"] = c.Stop
		}
	}
	if mi.ToolChoice != nil && *mi.ToolChoice != "" {
		params["tool_choice"] = string(*mi.ToolChoice)
	}
	if len(params) > 0 {
		if b, err := json.Marshal(params); err == nil {
			kvs = append(kvs, attribute.String(GenAIRequestParameters, string(b)))
		}
	}
	for i, t := range mi.Tools {
		if s := toolDefinition(t); s != "" {
			kvs = append(kvs, attribute.String(GenAIToolDefinitions+"."+strconv.Itoa(i)+"."+ToolJSONSchema, s))
		}
	}
	kvs = append(kvs, messageAttributes(GenAIInputMessages, mi.Messages)...)
	kvs = append(kvs, messagesValue(InputValue, InputMimeType, mi.Messages)...)
	return kvs
}

// toolDefinition is the OpenAI-style function definition of an Eino tool.
func toolDefinition(t *schema.ToolInfo) string {
	if t == nil || t.Name == "" {
		return ""
	}
	fn := map[string]any{"name": t.Name}
	if t.Desc != "" {
		fn["description"] = t.Desc
	}
	if t.ParamsOneOf != nil {
		if js, err := t.ParamsOneOf.ToJSONSchema(); err == nil && js != nil {
			fn["parameters"] = js
		}
	}
	b, err := json.Marshal(map[string]any{"type": "function", "function": fn})
	if err != nil {
		return ""
	}
	return truncate(string(b), maxValueLen)
}

// outputAttributes maps a callback output to span attributes.
func (h *Handler) outputAttributes(st *runState, output callbacks.CallbackOutput) []attribute.KeyValue {
	if output == nil {
		return nil
	}
	switch st.kind {
	case SpanKindLLM:
		if mo := model.ConvCallbackOutput(output); mo != nil {
			kvs := modelUsageAttributes(mo.TokenUsage, mo.Message)
			if mo.Message != nil {
				kvs = append(kvs, outputMessageAttributes(mo.Message)...)
			}
			return kvs
		}
	case SpanKindTool:
		if to := tool.ConvCallbackOutput(output); to != nil {
			res := to.Response
			if res == "" && to.ToolOutput != nil {
				res = toolResultText(to.ToolOutput)
			}
			res = truncate(res, maxValueLen)
			return []attribute.KeyValue{
				attribute.String(GenAIToolCallResult, res),
				attribute.String(OutputValue, res),
				attribute.String(OutputMimeType, mimeOf(res)),
			}
		}
	case SpanKindRetriever:
		if ro := retriever.ConvCallbackOutput(output); ro != nil {
			return documentAttributes(ro.Docs)
		}
	case SpanKindEmbedding:
		if eo := embedding.ConvCallbackOutput(output); eo != nil {
			kvs := []attribute.KeyValue{attribute.Int(EinoEmbeddingCount, len(eo.Embeddings))}
			if len(eo.Embeddings) > 0 && len(eo.Embeddings[0]) > 0 {
				kvs = append(kvs, attribute.Int(GenAIEmbeddingsDimension, len(eo.Embeddings[0])))
			}
			if u := eo.TokenUsage; u != nil && u.PromptTokens+u.CompletionTokens+u.TotalTokens > 0 {
				kvs = append(kvs,
					attribute.Int(GenAIUsageInputTokens, u.PromptTokens),
					attribute.Int(GenAIUsageTotalTokens, u.TotalTokens))
			}
			if eo.Config != nil && eo.Config.Model != "" {
				kvs = append(kvs, attribute.String(GenAIRequestModel, eo.Config.Model))
			}
			// The vectors are never exported.
			return kvs
		}
	}
	if st.info != nil {
		switch st.info.Component {
		case components.ComponentOfPrompt:
			if po := prompt.ConvCallbackOutput(output); po != nil {
				return messagesValue(OutputValue, OutputMimeType, po.Result)
			}
		case components.ComponentOfIndexer:
			if io := indexer.ConvCallbackOutput(output); io != nil {
				return []attribute.KeyValue{attribute.Int(EinoIndexerDocumentCount, len(io.IDs))}
			}
		}
	}
	return valueAttributes(OutputValue, OutputMimeType, output)
}

// modelUsageAttributes writes token usage from the callback payload: the
// model's TokenUsage, or the usage on the returned message. Nothing is
// written when neither reports a non-zero count; 0 is never a placeholder.
func modelUsageAttributes(u *model.TokenUsage, msg *schema.Message) []attribute.KeyValue {
	var kvs []attribute.KeyValue
	if msg != nil && msg.ResponseMeta != nil && msg.ResponseMeta.FinishReason != "" {
		kvs = append(kvs, attribute.StringSlice(GenAIResponseFinishReasons, []string{msg.ResponseMeta.FinishReason}))
	}
	prompt, completion, total, cached, reasoning := 0, 0, 0, 0, 0
	switch {
	case u != nil && u.PromptTokens+u.CompletionTokens+u.TotalTokens > 0:
		prompt, completion, total = u.PromptTokens, u.CompletionTokens, u.TotalTokens
		cached, reasoning = u.PromptTokenDetails.CachedTokens, u.CompletionTokensDetails.ReasoningTokens
	case msg != nil && msg.ResponseMeta != nil && msg.ResponseMeta.Usage != nil:
		mu := msg.ResponseMeta.Usage
		prompt, completion, total = mu.PromptTokens, mu.CompletionTokens, mu.TotalTokens
		cached, reasoning = mu.PromptTokenDetails.CachedTokens, mu.CompletionTokensDetails.ReasoningTokens
	}
	if prompt+completion+total == 0 {
		return kvs
	}
	if total == 0 {
		total = prompt + completion
	}
	kvs = append(kvs,
		attribute.Int(GenAIUsageInputTokens, prompt),
		attribute.Int(GenAIUsageOutputTokens, completion),
		attribute.Int(GenAIUsageTotalTokens, total))
	if cached > 0 {
		kvs = append(kvs, attribute.Int(GenAIUsageCacheReadTokens, cached))
	}
	if reasoning > 0 {
		kvs = append(kvs, attribute.Int(GenAIUsageReasoningTokens, reasoning))
	}
	return kvs
}

// outputMessageAttributes is a model's reply: the gen_ai.output.messages.*
// keys and output.value.
func outputMessageAttributes(m *schema.Message) []attribute.KeyValue {
	return append(messageAttributes(GenAIOutputMessages, []*schema.Message{m}), messageValue(OutputValue, OutputMimeType, m)...)
}

// messageValue is one message as a value: its text when it is plain text,
// otherwise the message as JSON. Graph, chain and agent spans get this, not
// the gen_ai.*.messages keys, which belong to model spans.
func messageValue(valueKey, mimeKey string, m *schema.Message) []attribute.KeyValue {
	if m.Content != "" && len(m.ToolCalls) == 0 && len(m.AssistantGenMultiContent) == 0 && len(m.MultiContent) == 0 {
		return []attribute.KeyValue{
			attribute.String(valueKey, truncate(m.Content, maxValueLen)),
			attribute.String(mimeKey, MimeTypeText),
		}
	}
	return messagesValue(valueKey, mimeKey, []*schema.Message{m})
}

func documentAttributes(docs []*schema.Document) []attribute.KeyValue {
	kvs := []attribute.KeyValue{attribute.Int(EinoRetrieverDocumentCount, len(docs))}
	for i, d := range docs {
		if d == nil {
			continue
		}
		p := RetrievalDocuments + "." + strconv.Itoa(i) + "."
		if d.ID != "" {
			kvs = append(kvs, attribute.String(p+DocumentID, d.ID))
		}
		if d.MetaData != nil {
			if _, ok := d.MetaData["_score"]; ok {
				kvs = append(kvs, attribute.Float64(p+DocumentScore, d.Score()))
			}
		}
		kvs = append(kvs, attribute.String(p+DocumentContent, truncate(d.Content, maxValueLen)))
	}
	return kvs
}

func toolResultText(r *schema.ToolResult) string {
	if r == nil {
		return ""
	}
	var parts []string
	for _, p := range r.Parts {
		if p.Text != "" {
			parts = append(parts, p.Text)
		}
	}
	return strings.Join(parts, "\n")
}

// messageAttributes flattens messages into the traceAI
// {prefix}.{i}.message.* keys. Image URLs are kept (data: URLs only up to the
// TraceConfig limit); audio, video and file bytes are never copied.
func messageAttributes(prefix string, msgs []*schema.Message) []attribute.KeyValue {
	var kvs []attribute.KeyValue
	n := 0
	for _, m := range msgs {
		if m == nil {
			continue
		}
		p := prefix + "." + strconv.Itoa(n) + "."
		n++
		if m.Role != "" {
			kvs = append(kvs, attribute.String(p+MessageRole, string(m.Role)))
		}
		if m.Name != "" {
			kvs = append(kvs, attribute.String(p+MessageName, m.Name))
		}
		if m.ToolCallID != "" {
			kvs = append(kvs, attribute.String(p+MessageToolCallID, m.ToolCallID))
		}
		if m.Content != "" {
			kvs = append(kvs, attribute.String(p+MessageContent, truncate(m.Content, maxValueLen)))
		}
		k := 0
		part := func(typ, text, image string) {
			pp := p + MessageContents + "." + strconv.Itoa(k) + "."
			k++
			kvs = append(kvs, attribute.String(pp+MessageContentType, typ))
			if text != "" {
				kvs = append(kvs, attribute.String(pp+MessageContentText, truncate(text, maxValueLen)))
			}
			if image != "" {
				kvs = append(kvs, attribute.String(pp+MessageContentImage, image))
			}
		}
		for _, mp := range m.UserInputMultiContent {
			switch mp.Type {
			case schema.ChatMessagePartTypeText:
				part("text", mp.Text, "")
			case schema.ChatMessagePartTypeImageURL:
				part("image", "", inputImage(mp.Image))
			default:
				part(partType(mp.Type), "", "")
			}
		}
		for _, mp := range m.AssistantGenMultiContent {
			switch mp.Type {
			case schema.ChatMessagePartTypeText:
				part("text", mp.Text, "")
			case schema.ChatMessagePartTypeImageURL:
				part("image", "", outputImage(mp.Image))
			default:
				part(partType(mp.Type), "", "")
			}
		}
		for _, mp := range m.MultiContent { // older components still fill MultiContent
			switch mp.Type {
			case schema.ChatMessagePartTypeText:
				part("text", mp.Text, "")
			case schema.ChatMessagePartTypeImageURL:
				url := ""
				if mp.ImageURL != nil {
					url = mp.ImageURL.URL
				}
				part("image", "", url)
			default:
				part(partType(mp.Type), "", "")
			}
		}
		for j, tc := range m.ToolCalls {
			tp := p + MessageToolCalls + "." + strconv.Itoa(j) + "."
			if tc.ID != "" {
				kvs = append(kvs, attribute.String(tp+ToolCallID, tc.ID))
			}
			if tc.Function.Name != "" {
				kvs = append(kvs, attribute.String(tp+ToolCallFunctionName, tc.Function.Name))
			}
			if tc.Function.Arguments != "" {
				kvs = append(kvs, attribute.String(tp+ToolCallFunctionArguments, truncate(tc.Function.Arguments, maxValueLen)))
			}
		}
	}
	return kvs
}

func partType(t schema.ChatMessagePartType) string {
	s := strings.TrimSuffix(string(t), "_url")
	if s == "" {
		return "unknown"
	}
	return s
}

// inputImage returns an image part's URL, or a data: URL built from inline
// base64 data (dropped later by the TraceConfig when it is too long).
func inputImage(img *schema.MessageInputImage) string {
	if img == nil {
		return ""
	}
	return imageRef(img.URL, img.Base64Data, img.MIMEType)
}

func outputImage(img *schema.MessageOutputImage) string {
	if img == nil {
		return ""
	}
	return imageRef(img.URL, img.Base64Data, img.MIMEType)
}

func imageRef(url, b64 *string, mime string) string {
	if url != nil && *url != "" {
		return *url
	}
	if b64 != nil && *b64 != "" {
		if mime == "" {
			mime = "image/png"
		}
		return "data:" + mime + ";base64," + *b64
	}
	return ""
}

type simpleToolCall struct {
	ID        string `json:"id,omitempty"`
	Name      string `json:"name,omitempty"`
	Arguments string `json:"arguments,omitempty"`
}

type simpleMessage struct {
	Role       string           `json:"role,omitempty"`
	Name       string           `json:"name,omitempty"`
	Content    string           `json:"content,omitempty"`
	Parts      []string         `json:"parts,omitempty"`
	ToolCalls  []simpleToolCall `json:"tool_calls,omitempty"`
	ToolCallID string           `json:"tool_call_id,omitempty"`
}

// messagesValue is the conversation as JSON for input.value / output.value:
// role, text, part types, tool calls and tool results. Image, audio, video
// and file data are left out.
func messagesValue(valueKey, mimeKey string, msgs []*schema.Message) []attribute.KeyValue {
	out := make([]simpleMessage, 0, len(msgs))
	for _, m := range msgs {
		if m == nil {
			continue
		}
		sm := simpleMessage{Role: string(m.Role), Name: m.Name, Content: m.Content, ToolCallID: m.ToolCallID}
		for _, p := range m.UserInputMultiContent {
			sm.Parts = append(sm.Parts, partText(string(p.Type), p.Text))
		}
		for _, p := range m.AssistantGenMultiContent {
			sm.Parts = append(sm.Parts, partText(string(p.Type), p.Text))
		}
		for _, p := range m.MultiContent { // see messageAttributes
			sm.Parts = append(sm.Parts, partText(string(p.Type), p.Text))
		}
		for _, tc := range m.ToolCalls {
			sm.ToolCalls = append(sm.ToolCalls, simpleToolCall{ID: tc.ID, Name: tc.Function.Name, Arguments: tc.Function.Arguments})
		}
		out = append(out, sm)
	}
	if len(out) == 0 {
		return nil
	}
	b, err := json.Marshal(out)
	if err != nil {
		return nil
	}
	return []attribute.KeyValue{
		attribute.String(valueKey, truncate(string(b), maxValueLen)),
		attribute.String(mimeKey, MimeTypeJSON),
	}
}

func partText(typ, text string) string {
	if typ == string(schema.ChatMessagePartTypeText) {
		return text
	}
	return "[" + strings.TrimSuffix(typ, "_url") + "]"
}

// valueAttributes writes v as input.value / output.value: a string as text,
// messages as the conversation, anything else as JSON. Values that cannot be
// encoded (functions, channels) are skipped.
func valueAttributes(valueKey, mimeKey string, v any) []attribute.KeyValue {
	switch t := v.(type) {
	case nil:
		return nil
	case string:
		if t == "" {
			return nil
		}
		return []attribute.KeyValue{
			attribute.String(valueKey, truncate(t, maxValueLen)),
			attribute.String(mimeKey, MimeTypeText),
		}
	case *schema.Message:
		if t == nil {
			return nil
		}
		return messageValue(valueKey, mimeKey, t)
	case []*schema.Message:
		return messagesValue(valueKey, mimeKey, t)
	}
	b, err := json.Marshal(v)
	if err != nil || len(b) == 0 || string(b) == "null" || string(b) == "{}" {
		return nil
	}
	return []attribute.KeyValue{
		attribute.String(valueKey, truncate(string(b), maxValueLen)),
		attribute.String(mimeKey, MimeTypeJSON),
	}
}

func mimeOf(s string) string {
	t := strings.TrimSpace(s)
	if (strings.HasPrefix(t, "{") || strings.HasPrefix(t, "[")) && json.Valid([]byte(t)) {
		return MimeTypeJSON
	}
	return MimeTypeText
}

func roundFloat(f float32) float64 {
	v, _ := strconv.ParseFloat(strconv.FormatFloat(float64(f), 'g', 6, 32), 64)
	return v
}

// truncate cuts s to at most n bytes without splitting a UTF-8 rune.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	cut := n
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut]
}
