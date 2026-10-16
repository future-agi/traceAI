package traceaieino

import (
	"strings"

	"github.com/cloudwego/eino/adk"
	"github.com/cloudwego/eino/callbacks"
	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/schema"
	"go.opentelemetry.io/otel/attribute"
)

// streamAccumulator collects what the span of a streamed output needs.
type streamAccumulator struct {
	kind     string
	keepText bool
	msgs     []*schema.Message
	usage    *model.TokenUsage
	other    []callbacks.CallbackOutput
	chunks   int
}

func newStreamAccumulator(kind string, tc TraceConfig) *streamAccumulator {
	return &streamAccumulator{kind: kind, keepText: !tc.HideOutputs}
}

func (a *streamAccumulator) add(chunk callbacks.CallbackOutput) {
	a.chunks++
	if a.kind == SpanKindLLM {
		mo := model.ConvCallbackOutput(chunk)
		if mo == nil {
			return
		}
		// Usage: the chunk that reports it (OpenAI-compatible models send it
		// once, in the last chunk). Providers that repeat a running total
		// are covered by keeping the largest total. Chunks are never summed.
		u := mo.TokenUsage
		if u == nil && mo.Message != nil && mo.Message.ResponseMeta != nil && mo.Message.ResponseMeta.Usage != nil {
			mu := mo.Message.ResponseMeta.Usage
			u = &model.TokenUsage{
				PromptTokens: mu.PromptTokens, CompletionTokens: mu.CompletionTokens, TotalTokens: mu.TotalTokens,
				PromptTokenDetails:      model.PromptTokenDetails{CachedTokens: mu.PromptTokenDetails.CachedTokens},
				CompletionTokensDetails: model.CompletionTokensDetails{ReasoningTokens: mu.CompletionTokensDetails.ReasoningTokens},
			}
		}
		if u != nil && u.PromptTokens+u.CompletionTokens+u.TotalTokens > 0 &&
			(a.usage == nil || usageTotal(u) >= usageTotal(a.usage)) {
			a.usage = u
		}
		if mo.Message != nil {
			a.msgs = append(a.msgs, mo.Message)
		}
		return
	}
	if len(a.other) < maxStreamChunks {
		a.other = append(a.other, chunk)
	}
}

func usageTotal(u *model.TokenUsage) int {
	if u.TotalTokens > 0 {
		return u.TotalTokens
	}
	return u.PromptTokens + u.CompletionTokens
}

func (a *streamAccumulator) attributes(h *Handler) []attribute.KeyValue {
	if a.kind == SpanKindLLM {
		msg := joinMessages(a.msgs)
		kvs := modelUsageAttributes(a.usage, nil)
		if msg != nil && msg.ResponseMeta != nil && msg.ResponseMeta.FinishReason != "" {
			kvs = append(kvs, attribute.StringSlice(GenAIResponseFinishReasons, []string{msg.ResponseMeta.FinishReason}))
		}
		if msg != nil {
			kvs = append(kvs, outputMessageAttributes(msg)...)
		}
		return kvs
	}
	if !a.keepText || len(a.other) == 0 {
		return nil
	}
	if msgs, ok := allMessages(a.other); ok {
		if m := joinMessages(msgs); m != nil {
			return messageValue(OutputValue, OutputMimeType, m)
		}
		return nil
	}
	if len(a.other) == 1 {
		return valueAttributes(OutputValue, OutputMimeType, a.other[0])
	}
	return valueAttributes(OutputValue, OutputMimeType, a.other)
}

// streamInputAttributes maps the chunks of a streamed input.
func (h *Handler) streamInputAttributes(st *runState, chunks []callbacks.CallbackInput) []attribute.KeyValue {
	if len(chunks) == 0 {
		return nil
	}
	if st.kind == SpanKindLLM {
		var msgs []*schema.Message
		for _, c := range chunks {
			if mi := model.ConvCallbackInput(c); mi != nil {
				msgs = append(msgs, mi.Messages...)
			}
		}
		if len(msgs) > 0 {
			return append(messageAttributes(GenAIInputMessages, msgs), messagesValue(InputValue, InputMimeType, msgs)...)
		}
	}
	if len(chunks) == 1 {
		return valueAttributes(InputValue, InputMimeType, chunks[0])
	}
	return valueAttributes(InputValue, InputMimeType, chunks)
}

// drainAgentEvents reads the handler's copy of an ADK agent's event stream to
// the end, closing every message stream in it, then ends the agent span: ERROR
// when an event carried an error, with the last message as the output.
func drainAgentEvents[M adk.MessageType](h *Handler, st *runState, events *adk.AsyncIterator[*adk.TypedAgentEvent[M]]) {
	traced := st != nil && st.span != nil && !st.ended.Load()
	keep := traced && !h.tc.HideOutputs
	var (
		last        any
		runErr      error
		interrupted bool
	)
	for {
		ev, ok := events.Next()
		if !ok {
			break
		}
		if ev == nil {
			continue
		}
		if ev.Err != nil {
			runErr = ev.Err
		}
		if ev.Action != nil && ev.Action.Interrupted != nil {
			interrupted = true
		}
		if ev.Output == nil || ev.Output.MessageOutput == nil {
			continue
		}
		mv := ev.Output.MessageOutput
		if mv.IsStreaming {
			if mv.MessageStream == nil {
				continue
			}
			if !keep {
				mv.MessageStream.Close()
				continue
			}
			if m := concatStream(mv.MessageStream); m != nil {
				last = m
			}
			continue
		}
		var zero M
		if keep && any(mv.Message) != any(zero) {
			last = mv.Message
		}
	}
	if !traced {
		return
	}
	var kvs []attribute.KeyValue
	if interrupted {
		kvs = append(kvs, attribute.Bool(EinoAgentInterrupted, true))
	}
	switch m := last.(type) {
	case *schema.Message:
		if m != nil {
			kvs = append(kvs, messageValue(OutputValue, OutputMimeType, m)...)
		}
	case nil:
	default:
		kvs = append(kvs, valueAttributes(OutputValue, OutputMimeType, m)...)
	}
	h.finish(st, kvs, runErr)
}

// concatStream reads a message stream to the end and joins its chunks. It
// always closes the stream.
func concatStream[M adk.MessageType](s *schema.StreamReader[M]) any {
	chunks := readStream(s, 1<<16)
	if len(chunks) == 0 {
		return nil
	}
	switch any(chunks[0]).(type) {
	case *schema.Message:
		msgs := make([]*schema.Message, 0, len(chunks))
		for _, c := range chunks {
			if m, ok := any(c).(*schema.Message); ok && m != nil {
				msgs = append(msgs, m)
			}
		}
		if m := joinMessages(msgs); m != nil {
			return m
		}
		return nil
	}
	return chunks[len(chunks)-1]
}

func allMessages(chunks []callbacks.CallbackOutput) ([]*schema.Message, bool) {
	msgs := make([]*schema.Message, 0, len(chunks))
	for _, c := range chunks {
		m, ok := c.(*schema.Message)
		if !ok {
			return nil, false
		}
		if m != nil {
			msgs = append(msgs, m)
		}
	}
	return msgs, true
}

// joinMessages joins streamed message chunks into one message: text and parts
// appended, tool-call fragments merged by index, the last finish reason. It
// reads only the fields it copies and never Extra: Eino's ADK writes a message
// id into the first chunk's Extra after the stream has been copied to the
// callback handlers, so schema.ConcatMessages (which reads Extra) would race
// with it. It never sums token usage.
func joinMessages(chunks []*schema.Message) *schema.Message {
	var out *schema.Message
	var text strings.Builder
	index := map[int]int{}
	for _, c := range chunks {
		if c == nil {
			continue
		}
		if out == nil {
			out = &schema.Message{}
		}
		if out.Role == "" {
			out.Role = c.Role
		}
		if out.Name == "" {
			out.Name = c.Name
		}
		if out.ToolCallID == "" {
			out.ToolCallID = c.ToolCallID
		}
		text.WriteString(c.Content)
		out.UserInputMultiContent = append(out.UserInputMultiContent, c.UserInputMultiContent...)
		out.AssistantGenMultiContent = append(out.AssistantGenMultiContent, c.AssistantGenMultiContent...)
		for _, tc := range c.ToolCalls {
			if tc.Index != nil {
				if at, ok := index[*tc.Index]; ok {
					cur := &out.ToolCalls[at]
					if cur.ID == "" {
						cur.ID = tc.ID
					}
					if cur.Function.Name == "" {
						cur.Function.Name = tc.Function.Name
					}
					cur.Function.Arguments += tc.Function.Arguments
					continue
				}
				index[*tc.Index] = len(out.ToolCalls)
			}
			out.ToolCalls = append(out.ToolCalls, schema.ToolCall{Index: tc.Index, ID: tc.ID, Type: tc.Type,
				Function: schema.FunctionCall{Name: tc.Function.Name, Arguments: tc.Function.Arguments}})
		}
		if rm := c.ResponseMeta; rm != nil && rm.FinishReason != "" {
			out.ResponseMeta = &schema.ResponseMeta{FinishReason: rm.FinishReason}
		}
	}
	if out != nil {
		out.Content = text.String()
	}
	return out
}
