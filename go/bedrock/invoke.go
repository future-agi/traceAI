package traceaibedrock

import (
	"encoding/json"
	"strconv"
	"strings"

	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"go.opentelemetry.io/otel/attribute"
)

// Only the Bedrock Anthropic Messages schema is normalized. Never guess at
// model-specific bodies or capture arbitrary JSON from unknown providers.
type invocation struct {
	Version     string          `json:"anthropic_version"`
	Messages    []invokeMessage `json:"messages"`
	System      json.RawMessage `json:"system"`
	MaxTokens   *int64          `json:"max_tokens"`
	Temperature *float64        `json:"temperature"`
	TopP        *float64        `json:"top_p"`
	Tools       []invokeTool    `json:"tools"`
}
type invokeMessage struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}
type invokeBlock struct {
	Type      string          `json:"type"`
	Text      string          `json:"text"`
	ID        string          `json:"id"`
	ToolUseID string          `json:"tool_use_id"`
	Name      string          `json:"name"`
	Input     json.RawMessage `json:"input"`
	Content   json.RawMessage `json:"content"`
}
type invokeTool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"input_schema"`
}
type invokeUsage struct {
	Input      *int64 `json:"input_tokens"`
	Output     *int64 `json:"output_tokens"`
	CacheRead  *int64 `json:"cache_read_input_tokens"`
	CacheWrite *int64 `json:"cache_creation_input_tokens"`
}

func parseInvocation(model *string, body []byte) *invocation {
	if model == nil || len(body) > 2*maxCapture {
		return nil
	}
	// Match only the final resource component, including regional inference
	// profiles. Opaque provisioned model/profile ARNs cannot identify a schema.
	id := *model
	if i := strings.LastIndex(id, "/"); i >= 0 {
		id = id[i+1:]
	}
	for _, prefix := range []string{"us.", "eu.", "apac.", "global.", "au.", "jp."} {
		id = strings.TrimPrefix(id, prefix)
	}
	if !strings.HasPrefix(id, "anthropic.") {
		return nil
	}
	var in invocation
	if json.Unmarshal(body, &in) != nil || in.Version != "bedrock-2023-05-31" || in.Messages == nil {
		return nil
	}
	return &in
}
func (r *record) invokeBlocks(raw json.RawMessage) []block {
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return []block{{Type: "text", Text: r.take(text)}}
	}
	var content []invokeBlock
	if json.Unmarshal(raw, &content) != nil {
		return nil
	}
	out := make([]block, 0)
	for i, b := range content {
		if i >= maxBlocks {
			r.truncated = true
			break
		}
		switch b.Type {
		case "text":
			out = append(out, block{Type: "text", Text: r.take(b.Text)})
		case "tool_use":
			out = append(out, block{Type: b.Type, ID: r.take(b.ID), Name: r.take(b.Name), Arguments: r.take(string(b.Input))})
		case "tool_result":
			result := block{Type: b.Type, ID: r.take(b.ToolUseID)}
			// Tool results may hold media or nested text. Only plain text is retained.
			var value string
			if json.Unmarshal(b.Content, &value) == nil {
				result.Text = r.take(value)
			} else {
				var parts []invokeBlock
				if json.Unmarshal(b.Content, &parts) == nil {
					for j, p := range parts {
						if j >= maxBlocks {
							break
						}
						if p.Type == "text" {
							result.Text += r.take(p.Text)
						}
					}
				}
			}
			out = append(out, result)
		}
	}
	return out
}
func (r *record) invokeInput(in *invocation) {
	if !r.privacy.HideLLMInvocationParameters {
		if in.MaxTokens != nil {
			r.span.SetAttributes(attribute.Int64("gen_ai.request.max_tokens", *in.MaxTokens))
		}
		if in.Temperature != nil {
			r.span.SetAttributes(attribute.Float64("gen_ai.request.temperature", *in.Temperature))
		}
		if in.TopP != nil {
			r.span.SetAttributes(attribute.Float64("gen_ai.request.top_p", *in.TopP))
		}
	}
	if r.privacy.HideInputs {
		r.span.SetAttributes(attribute.String("input.value", "__REDACTED__"))
	}
	if !r.privacy.input() {
		return
	}
	ms := make([]message, 0)
	if len(in.System) > 0 {
		ms = append(ms, message{Role: "system", Content: r.invokeBlocks(in.System)})
	}
	for i, m := range in.Messages {
		if i >= 32 {
			r.truncated = true
			break
		}
		ms = append(ms, message{Role: bounded(m.Role, 32), Content: r.invokeBlocks(m.Content)})
	}
	r.messages("input", ms)
	for i, t := range in.Tools {
		if i >= 16 {
			r.truncated = true
			break
		}
		projected := invokeTool{Name: r.take(t.Name), Description: r.take(t.Description)}
		if len(t.InputSchema) <= maxCapture-r.used {
			projected.InputSchema = json.RawMessage(r.take(string(t.InputSchema)))
		} else {
			r.truncated = true
		}
		b, _ := json.Marshal(projected)
		r.contentAttrs(attribute.String("gen_ai.tool.definitions."+strconv.Itoa(i)+".tool.json_schema", bounded(string(b), maxCapture)))
	}
	r.used = 0
	r.contentAttributes = 0
}
func (r *record) invokeUsage(u *invokeUsage) {
	if u == nil {
		return
	}
	if u.Input != nil {
		r.usage.Input = u.Input
	}
	if u.Output != nil {
		r.usage.Output = u.Output
	}
	if u.CacheRead != nil {
		r.usage.CacheRead = u.CacheRead
	}
	if u.CacheWrite != nil {
		r.usage.CacheWrite = u.CacheWrite
	}
	// Anthropic input_tokens excludes cache reads/writes. Keep the authoritative
	// counters separately; do not invent a total when fields are absent.
}

type invokeResponse struct {
	Content    json.RawMessage `json:"content"`
	Usage      *invokeUsage    `json:"usage"`
	StopReason string          `json:"stop_reason"`
}

// Metadata is independent of the content budget. Unmarshal skips unknown fields
// (including content) without copying their values; never decode the entire
// response into a map or RawMessage merely to read usage from a large completion.
// Bound stop_reason too, so a malformed response cannot allocate arbitrary text.
type stopReason string

func (s *stopReason) UnmarshalJSON(data []byte) error {
	*s = ""
	if len(data) > 512 {
		return nil
	}
	var value string
	if err := json.Unmarshal(data, &value); err != nil {
		return err
	}
	*s = stopReason(value)
	return nil
}

func (r *record) invokeOutput(data []byte) {
	var metadata struct {
		Usage      *invokeUsage `json:"usage"`
		StopReason stopReason   `json:"stop_reason"`
	}
	if json.Unmarshal(data, &metadata) != nil {
		return
	}
	r.invokeUsage(metadata.Usage)
	r.stop(string(metadata.StopReason))
	if !r.privacy.output() {
		return
	}
	if len(data) > 2*maxCapture {
		r.truncated = true
		return
	}
	var out invokeResponse
	if json.Unmarshal(data, &out) != nil {
		return
	}
	for i, b := range r.invokeBlocks(out.Content) {
		copy := b
		r.blocks[int32(i)] = &copy
	}
}
func (r *record) invokeEvent(ev types.ResponseStream) {
	v, ok := ev.(*types.ResponseStreamMemberChunk)
	if !ok {
		return
	}
	if len(v.Value.Bytes) > 2*maxCapture {
		r.truncated = true
		return
	}
	var e struct {
		Type         string          `json:"type"`
		Index        int32           `json:"index"`
		Message      *invokeResponse `json:"message"`
		ContentBlock *invokeBlock    `json:"content_block"`
		Delta        struct {
			Type        string `json:"type"`
			Text        string `json:"text"`
			PartialJSON string `json:"partial_json"`
			StopReason  string `json:"stop_reason"`
		} `json:"delta"`
		Usage *invokeUsage `json:"usage"`
	}
	if json.Unmarshal(v.Value.Bytes, &e) != nil {
		return
	}
	switch e.Type {
	case "message_start":
		if e.Message != nil {
			r.invokeUsage(e.Message.Usage)
		}
	case "message_delta":
		r.invokeUsage(e.Usage)
		r.stop(e.Delta.StopReason)
	case "content_block_start":
		if e.ContentBlock != nil {
			b := e.ContentBlock
			if b.Type == "text" {
				r.delta(e.Index, "text", b.Text)
			} else if b.Type == "tool_use" && r.privacy.output() {
				if dst := r.ensureBlock(e.Index, "tool_use"); dst != nil {
					dst.ID = r.take(b.ID)
					dst.Name = r.take(b.Name)
				}
			}
		}
	case "content_block_delta":
		switch e.Delta.Type {
		case "text_delta":
			r.delta(e.Index, "text", e.Delta.Text)
		case "input_json_delta":
			r.delta(e.Index, "tool_use", e.Delta.PartialJSON)
		}
	}
}
