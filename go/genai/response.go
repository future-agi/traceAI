package traceaigenai

import (
	"encoding/json"
	"sort"
	"strconv"

	"go.opentelemetry.io/otel/attribute"
)

// usageMetadata is GenerateContentResponse.usageMetadata. Every count is a
// pointer: the API leaves zero counts out, and an absent count is never
// written as 0.
type usageMetadata struct {
	PromptTokenCount        *int64 `json:"promptTokenCount"`
	CandidatesTokenCount    *int64 `json:"candidatesTokenCount"`
	TotalTokenCount         *int64 `json:"totalTokenCount"`
	ThoughtsTokenCount      *int64 `json:"thoughtsTokenCount"`
	CachedContentTokenCount *int64 `json:"cachedContentTokenCount"`
}

type generateResponse struct {
	Candidates []struct {
		Index        *int    `json:"index"`
		Content      content `json:"content"`
		FinishReason string  `json:"finishReason"`
	} `json:"candidates"`
	UsageMetadata *usageMetadata  `json:"usageMetadata"`
	ModelVersion  string          `json:"modelVersion"`
	ResponseID    string          `json:"responseId"`
	Error         json.RawMessage `json:"error"`
}

type apiError struct {
	Code    int
	Message string
	Status  string
}

// parseAPIError reads the Google API error body
// {"error":{"code":404,"message":"...","status":"NOT_FOUND","details":[...]}}.
// details are never read.
func parseAPIError(b []byte) apiError {
	var e apiError
	var outer struct {
		Error json.RawMessage `json:"error"`
	}
	if json.Unmarshal(b, &outer) != nil || len(outer.Error) == 0 {
		return e
	}
	if s, ok := jsonString(outer.Error); ok {
		e.Message = s
		return e
	}
	var inner struct {
		Code    json.RawMessage `json:"code"`
		Message string          `json:"message"`
		Status  string          `json:"status"`
	}
	if json.Unmarshal(outer.Error, &inner) != nil {
		return e
	}
	if n, ok := jsonInt(inner.Code); ok {
		e.Code = int(n)
	}
	e.Message, e.Status = inner.Message, inner.Status
	return e
}

type candidateAcc struct {
	role   string
	parts  []part
	finish string
}

// accumulator collects one GenerateContentResponse, either from a full
// response body or from the chunks of a stream.
type accumulator struct {
	model, id  string
	candidates map[int]*candidateAcc
	usage      *usageMetadata
	streamErr  *apiError
}

func newAccumulator() *accumulator {
	return &accumulator{candidates: map[int]*candidateAcc{}}
}

func (a *accumulator) addResponse(b []byte) {
	var r generateResponse
	if json.Unmarshal(b, &r) != nil {
		return
	}
	if len(r.Error) > 0 && string(r.Error) != "null" {
		e := parseAPIError(b)
		a.streamErr = &e
		return
	}
	if r.ModelVersion != "" {
		a.model = r.ModelVersion
	}
	if r.ResponseID != "" {
		a.id = r.ResponseID
	}
	// In a stream every chunk carries the running usage of the request, so
	// the last one wins; it is not summed.
	if r.UsageMetadata != nil {
		a.usage = r.UsageMetadata
	}
	for pos, c := range r.Candidates {
		idx := pos
		if c.Index != nil {
			idx = *c.Index
		}
		acc, ok := a.candidates[idx]
		if !ok {
			acc = &candidateAcc{}
			a.candidates[idx] = acc
		}
		if c.Content.Role != "" {
			acc.role = c.Content.Role
		}
		acc.parts = append(acc.parts, c.Content.Parts...)
		if c.FinishReason != "" {
			acc.finish = c.FinishReason
		}
	}
}

// attributes returns the response attributes. Usage is written only when the
// API reported it; nothing is written as a zero placeholder.
func (a *accumulator) attributes() []attribute.KeyValue {
	var kvs []attribute.KeyValue
	if a.id != "" {
		kvs = append(kvs, attribute.String(GenAIResponseID, a.id))
	}
	if a.model != "" {
		kvs = append(kvs, attribute.String(GenAIResponseModel, a.model))
	}
	kvs = append(kvs, usageAttributes(a.usage)...)

	idxs := make([]int, 0, len(a.candidates))
	for i := range a.candidates {
		idxs = append(idxs, i)
	}
	sort.Ints(idxs)
	var finish []string
	msgs := make([]content, 0, len(idxs))
	for n, i := range idxs {
		c := a.candidates[i]
		if c.finish != "" {
			finish = append(finish, c.finish)
		}
		m := content{Role: c.role, Parts: mergeText(c.parts)}
		msgs = append(msgs, m)
		kvs = append(kvs, messageAttributes(GenAIOutputMessages+"."+strconv.Itoa(n)+".", m, "model", true)...)
	}
	if len(finish) > 0 {
		kvs = append(kvs, attribute.StringSlice(GenAIResponseFinishReasons, finish))
	}
	if len(msgs) == 1 {
		v := viewParts(msgs[0].Parts)
		if text, ok := v.onlyText(); ok && text != "" && len(v.calls) == 0 && len(v.results) == 0 {
			return append(kvs,
				attribute.String(OutputValue, text),
				attribute.String(OutputMimeType, MimeTypeText))
		}
	}
	if s := conversationJSON(msgs, "model", TraceConfig{}, true); s != "" && len(msgs) > 0 {
		kvs = append(kvs,
			attribute.String(OutputValue, s),
			attribute.String(OutputMimeType, MimeTypeJSON))
	}
	return kvs
}

// mergeText joins consecutive text parts (a stream sends the answer in many
// small parts) so a streamed answer reads as one text, like a non-streamed
// one.
func mergeText(parts []part) []part {
	out := make([]part, 0, len(parts))
	for _, p := range parts {
		isText := p.Text != nil && !p.Thought && p.InlineData == nil && p.FileData == nil &&
			p.FunctionCall == nil && p.FunctionResponse == nil
		if isText && *p.Text == "" {
			continue
		}
		if n := len(out); isText && n > 0 {
			last := out[n-1]
			if last.Text != nil && !last.Thought && last.FunctionCall == nil && last.FunctionResponse == nil &&
				last.InlineData == nil && last.FileData == nil {
				s := *last.Text + *p.Text
				out[n-1].Text = &s
				continue
			}
		}
		out = append(out, p)
	}
	return out
}

// usageAttributes maps usageMetadata the way traceai-google-genai does:
// output tokens are candidates plus thoughts, thoughts are also the reasoning
// count, cached content tokens are cache reads.
func usageAttributes(u *usageMetadata) []attribute.KeyValue {
	if u == nil {
		return nil
	}
	var kvs []attribute.KeyValue
	if u.PromptTokenCount != nil {
		kvs = append(kvs, attribute.Int64(GenAIUsageInputTokens, *u.PromptTokenCount))
	}
	if u.CandidatesTokenCount != nil || u.ThoughtsTokenCount != nil {
		var out int64
		if u.CandidatesTokenCount != nil {
			out += *u.CandidatesTokenCount
		}
		if u.ThoughtsTokenCount != nil {
			out += *u.ThoughtsTokenCount
		}
		kvs = append(kvs, attribute.Int64(GenAIUsageOutputTokens, out))
	}
	if u.TotalTokenCount != nil {
		kvs = append(kvs, attribute.Int64(GenAIUsageTotalTokens, *u.TotalTokenCount))
	}
	if u.ThoughtsTokenCount != nil && *u.ThoughtsTokenCount > 0 {
		kvs = append(kvs, attribute.Int64(GenAIUsageReasoningTokens, *u.ThoughtsTokenCount))
	}
	if u.CachedContentTokenCount != nil && *u.CachedContentTokenCount > 0 {
		kvs = append(kvs, attribute.Int64(GenAIUsageCacheReadTokens, *u.CachedContentTokenCount))
	}
	return kvs
}
