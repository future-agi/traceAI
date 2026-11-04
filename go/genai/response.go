package traceaigenai

import (
	"encoding/json"
	"sort"

	"go.opentelemetry.io/otel/attribute"
)

// usageMetadata is GenerateContentResponse.usageMetadata. Every count is a
// pointer: the API leaves zero counts out, and an absent count is never
// written as 0.
type usageMetadata struct {
	PromptTokenCount           *int64            `json:"promptTokenCount"`
	ToolUsePromptTokenCount    *int64            `json:"toolUsePromptTokenCount"`
	CandidatesTokenCount       *int64            `json:"candidatesTokenCount"`
	TotalTokenCount            *int64            `json:"totalTokenCount"`
	ThoughtsTokenCount         *int64            `json:"thoughtsTokenCount"`
	CachedContentTokenCount    *int64            `json:"cachedContentTokenCount"`
	PromptTokensDetails        []*modalityTokens `json:"promptTokensDetails"`
	CandidatesTokensDetails    []*modalityTokens `json:"candidatesTokensDetails"`
	CacheTokensDetails         []*modalityTokens `json:"cacheTokensDetails"`
	ToolUsePromptTokensDetails []*modalityTokens `json:"toolUsePromptTokensDetails"`
}

type modalityTokens struct {
	Modality   string `json:"modality"`
	TokenCount *int64 `json:"tokenCount"`
}

type generateResponse struct {
	Candidates []struct {
		Index             *int               `json:"index"`
		Content           content            `json:"content"`
		FinishReason      string             `json:"finishReason"`
		GroundingMetadata *groundingMetadata `json:"groundingMetadata"`
		CitationMetadata  *citationMetadata  `json:"citationMetadata"`
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
	model, id    string
	candidates   map[int]*candidateAcc
	usage        *usageMetadata
	grounding    groundingCapture
	streamErr    *apiError
	decodeFailed bool
	stream       bool
}

func newAccumulator() *accumulator {
	return &accumulator{candidates: map[int]*candidateAcc{}}
}

func (a *accumulator) addResponse(b []byte) {
	normalized, err := normalizeGenerationResponse(b, a.stream)
	if err != nil {
		a.decodeFailed = true
		return
	}
	var r generateResponse
	if json.Unmarshal(normalized, &r) != nil {
		a.decodeFailed = true
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
		a.grounding.add(idx, c.GroundingMetadata, c.CitationMetadata)
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
func (a *accumulator) attributes(tc TraceConfig) []attribute.KeyValue {
	var kvs []attribute.KeyValue
	if a.id != "" {
		kvs = append(kvs, attribute.String(GenAIResponseID, a.id))
	}
	if a.model != "" {
		kvs = append(kvs, attribute.String(GenAIResponseModel, a.model))
	}
	kvs = append(kvs, usageAttributes(a.usage)...)
	if value := a.grounding.json(); value != "" {
		kvs = append(kvs, attribute.String(GenAIResponseGrounding, value))
	}

	idxs := make([]int, 0, len(a.candidates))
	for i := range a.candidates {
		idxs = append(idxs, i)
	}
	sort.Ints(idxs)
	var finish []string
	msgs := make([]content, 0, len(idxs))
	for _, i := range idxs {
		c := a.candidates[i]
		if c.finish != "" {
			finish = append(finish, c.finish)
		}
		m := content{Role: c.role, Parts: mergeText(c.parts)}
		msgs = append(msgs, m)
	}
	if len(finish) > 0 {
		kvs = append(kvs, attribute.StringSlice(GenAIResponseFinishReasons, finish))
	}
	preview := messagePreview(msgs, GenAIOutputMessages, "model", true)
	if len(msgs) == 1 {
		v := viewParts(msgs[0].Parts)
		if text, ok := v.onlyText(); ok && text != "" && len(v.calls) == 0 && len(v.results) == 0 {
			kvs = append(kvs,
				attribute.String(OutputValue, tc.contentText(text, true)),
				attribute.String(OutputMimeType, MimeTypeText))
			return append(kvs, preview...)
		}
	}
	if s := conversationJSON(msgs, "model", tc, true); s != "" && len(msgs) > 0 {
		kvs = append(kvs,
			attribute.String(OutputValue, s),
			attribute.String(OutputMimeType, MimeTypeJSON))
	}
	return append(kvs, preview...)
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

// Input tokens include tool-use prompts; output tokens include thoughts.
// Cache reads are a subset of prompt tokens, never added again. The provider
// total is authoritative and is not invented when it is absent.
func usageAttributes(u *usageMetadata) []attribute.KeyValue {
	if u == nil {
		return nil
	}
	var kvs []attribute.KeyValue
	if u.PromptTokenCount != nil || u.ToolUsePromptTokenCount != nil {
		var input int64
		if u.PromptTokenCount != nil {
			input += *u.PromptTokenCount
		}
		if u.ToolUsePromptTokenCount != nil {
			input += *u.ToolUsePromptTokenCount
			kvs = append(kvs, attribute.Int64(GenAIUsageToolUsePromptTokens, *u.ToolUsePromptTokenCount))
		}
		kvs = append(kvs, attribute.Int64(GenAIUsageInputTokens, input))
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
	if details := usageDetails(u); details != "" {
		kvs = append(kvs, attribute.String(GenAIUsageDetails, details))
	}
	return kvs
}

// Retain pricing-relevant modality counts in one attribute, without unbounded
// flattened keys or opaque provider metadata. Unknown enum strings are omitted.
func usageDetails(u *usageMetadata) string {
	out := map[string][]modalityTokens{}
	for key, details := range map[string][]*modalityTokens{
		"promptTokensDetails":        u.PromptTokensDetails,
		"candidatesTokensDetails":    u.CandidatesTokensDetails,
		"cacheTokensDetails":         u.CacheTokensDetails,
		"toolUsePromptTokensDetails": u.ToolUsePromptTokensDetails,
	} {
		for _, detail := range details {
			if detail == nil || detail.TokenCount == nil {
				continue
			}
			switch detail.Modality {
			case "MODALITY_UNSPECIFIED", "TEXT", "IMAGE", "VIDEO", "AUDIO", "DOCUMENT":
				out[key] = append(out[key], *detail)
			}
		}
	}
	if len(out) == 0 {
		return ""
	}
	b, _ := json.Marshal(out)
	return string(b)
}
