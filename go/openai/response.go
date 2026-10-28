package traceaiopenai

import (
	"encoding/json"
	"sort"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel/attribute"
)

type usage struct {
	PromptTokens        *int64 `json:"prompt_tokens"`
	CompletionTokens    *int64 `json:"completion_tokens"`
	TotalTokens         *int64 `json:"total_tokens"`
	PromptTokensDetails *struct {
		CachedTokens *int64 `json:"cached_tokens"`
	} `json:"prompt_tokens_details"`
	CompletionTokensDetails *struct {
		ReasoningTokens *int64 `json:"reasoning_tokens"`
	} `json:"completion_tokens_details"`
}

type outMessage struct {
	Role      string     `json:"role"`
	Content   *string    `json:"content"`
	Refusal   *string    `json:"refusal"`
	ToolCalls []toolCall `json:"tool_calls"`
}

type chatCompletion struct {
	ID      string `json:"id"`
	Model   string `json:"model"`
	Choices []struct {
		Index        int        `json:"index"`
		Message      outMessage `json:"message"`
		FinishReason *string    `json:"finish_reason"`
	} `json:"choices"`
	Usage *usage `json:"usage"`
}

type chatChunk struct {
	ID      string `json:"id"`
	Model   string `json:"model"`
	Choices []struct {
		Index        int        `json:"index"`
		Delta        outMessage `json:"delta"`
		FinishReason *string    `json:"finish_reason"`
	} `json:"choices"`
	Usage *usage          `json:"usage"`
	Error json.RawMessage `json:"error"`
}

type apiError struct {
	Message string
	Type    string
	Code    string
}

// parseAPIError reads {"error":{"message","type","code"}} and the looser
// shapes some OpenAI-compatible servers return.
func parseAPIError(b []byte) apiError {
	var e apiError
	var outer map[string]json.RawMessage
	if json.Unmarshal(b, &outer) != nil {
		return e
	}
	inner := outer
	if raw, ok := outer["error"]; ok {
		if s, ok := jsonString(raw); ok {
			e.Message = s
			return e
		}
		inner = nil
		_ = json.Unmarshal(raw, &inner)
	}
	e.Message, _ = jsonString(inner["message"])
	e.Type, _ = jsonString(inner["type"])
	if s, ok := jsonString(inner["code"]); ok {
		e.Code = s
	} else if n, ok := jsonInt(inner["code"]); ok {
		e.Code = strconv.FormatInt(n, 10)
	}
	return e
}

type choiceAcc struct {
	role       string
	content    strings.Builder
	hasContent bool
	finish     string
	calls      map[int]*callAcc
	callOrder  []int
}

type callAcc struct {
	id, name string
	args     strings.Builder
}

// accumulator collects one chat completion, either from a full response body
// or from its stream chunks.
type accumulator struct {
	id, model string
	choices   map[int]*choiceAcc
	usage     *usage
	streamErr *apiError
}

func newAccumulator() *accumulator {
	return &accumulator{choices: map[int]*choiceAcc{}}
}

func (a *accumulator) choice(i int) *choiceAcc {
	c, ok := a.choices[i]
	if !ok {
		c = &choiceAcc{calls: map[int]*callAcc{}}
		a.choices[i] = c
	}
	return c
}

func (a *accumulator) addCompletion(b []byte) {
	var cc chatCompletion
	if json.Unmarshal(b, &cc) != nil {
		return
	}
	a.id, a.model, a.usage = cc.ID, cc.Model, cc.Usage
	for _, ch := range cc.Choices {
		c := a.choice(ch.Index)
		c.addDelta(ch.Message, false)
		if ch.FinishReason != nil {
			c.finish = *ch.FinishReason
		}
	}
}

func (a *accumulator) addChunk(b []byte) {
	var ck chatChunk
	if json.Unmarshal(b, &ck) != nil {
		return
	}
	if len(ck.Error) > 0 && string(ck.Error) != "null" {
		e := parseAPIError(append(append([]byte(`{"error":`), ck.Error...), '}'))
		a.streamErr = &e
		return
	}
	if ck.ID != "" {
		a.id = ck.ID
	}
	if ck.Model != "" {
		a.model = ck.Model
	}
	if ck.Usage != nil {
		a.usage = ck.Usage
	}
	for _, ch := range ck.Choices {
		c := a.choice(ch.Index)
		c.addDelta(ch.Delta, true)
		if ch.FinishReason != nil && *ch.FinishReason != "" {
			c.finish = *ch.FinishReason
		}
	}
}

func (c *choiceAcc) addDelta(m outMessage, streaming bool) {
	if m.Role != "" {
		c.role = m.Role
	}
	if m.Content != nil && *m.Content != "" {
		c.content.WriteString(*m.Content)
		c.hasContent = true
	} else if m.Refusal != nil && *m.Refusal != "" {
		c.content.WriteString(*m.Refusal)
		c.hasContent = true
	}
	for pos, tcall := range m.ToolCalls {
		idx := pos
		if streaming && tcall.Index != nil {
			idx = *tcall.Index
		}
		call, ok := c.calls[idx]
		if !ok {
			call = &callAcc{}
			c.calls[idx] = call
			c.callOrder = append(c.callOrder, idx)
		}
		if tcall.ID != "" {
			call.id = tcall.ID
		}
		if tcall.Function.Name != "" {
			call.name = tcall.Function.Name
		}
		call.args.WriteString(tcall.Function.Arguments)
	}
}

// attributes returns the response attributes. Usage is written only when the
// API reported it; nothing is written as a zero placeholder.
func (a *accumulator) attributes(tc TraceConfig) []attribute.KeyValue {
	var kvs []attribute.KeyValue
	if a.id != "" {
		kvs = append(kvs, attribute.String(GenAIResponseID, a.id))
	}
	if a.model != "" {
		kvs = append(kvs, attribute.String(GenAIResponseModel, a.model))
	}
	if u := a.usage; u != nil {
		if u.PromptTokens != nil {
			kvs = append(kvs, attribute.Int64(GenAIUsageInputTokens, *u.PromptTokens))
		}
		if u.CompletionTokens != nil {
			kvs = append(kvs, attribute.Int64(GenAIUsageOutputTokens, *u.CompletionTokens))
		}
		if u.TotalTokens != nil {
			kvs = append(kvs, attribute.Int64(GenAIUsageTotalTokens, *u.TotalTokens))
		}
		if d := u.PromptTokensDetails; d != nil && d.CachedTokens != nil && *d.CachedTokens > 0 {
			kvs = append(kvs, attribute.Int64(GenAIUsageCacheReadTokens, *d.CachedTokens))
		}
		if d := u.CompletionTokensDetails; d != nil && d.ReasoningTokens != nil && *d.ReasoningTokens > 0 {
			kvs = append(kvs, attribute.Int64(GenAIUsageReasoningTokens, *d.ReasoningTokens))
		}
	}

	idxs := make([]int, 0, len(a.choices))
	for i := range a.choices {
		idxs = append(idxs, i)
	}
	sort.Ints(idxs)
	var finish []string
	type simpleOut struct {
		Role      string              `json:"role"`
		Content   string              `json:"content,omitempty"`
		ToolCalls []map[string]string `json:"tool_calls,omitempty"`
	}
	outs := make([]simpleOut, 0, len(idxs))
	anyCalls := false
	for n, i := range idxs {
		c := a.choices[i]
		if c.finish != "" {
			finish = append(finish, c.finish)
		}
		role := c.role
		if role == "" {
			role = "assistant"
		}
		prefix := GenAIOutputMessages + "." + strconv.Itoa(n) + "."
		kvs = append(kvs, attribute.String(prefix+MessageRole, role))
		so := simpleOut{Role: role}
		if c.hasContent {
			so.Content = c.content.String()
			kvs = append(kvs, attribute.String(prefix+MessageContent, so.Content))
		}
		calls := make([]toolCall, 0, len(c.callOrder))
		for _, ci := range c.callOrder {
			call := c.calls[ci]
			var t toolCall
			t.ID, t.Function.Name, t.Function.Arguments = call.id, call.name, call.args.String()
			calls = append(calls, t)
		}
		if len(calls) > 0 {
			anyCalls = true
			so.ToolCalls = simpleToolCalls(calls)
			kvs = append(kvs, toolCallAttributes(prefix, calls)...)
		}
		outs = append(outs, so)
	}
	if len(finish) > 0 {
		kvs = append(kvs, attribute.StringSlice(GenAIResponseFinishReasons, finish))
	}
	switch {
	case len(outs) == 1 && !anyCalls && outs[0].Content != "":
		kvs = append(kvs,
			attribute.String(OutputValue, outs[0].Content),
			attribute.String(OutputMimeType, MimeTypeText))
	case len(outs) > 0:
		if b, err := json.Marshal(outs); err == nil {
			kvs = append(kvs,
				attribute.String(OutputValue, string(b)),
				attribute.String(OutputMimeType, MimeTypeJSON))
		}
	}
	return kvs
}
