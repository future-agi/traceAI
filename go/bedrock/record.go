package traceaibedrock

import (
	"encoding/json"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

const maxCapture = 32 << 10
const maxBlocks = 64

// TraceConfig controls content at its source, before any attributes are built.
// Text/message switches also hide tool arguments, results, system prompts and
// definitions, rather than letting them leak through a duplicate JSON attribute.
// Binary/media, documents, reasoning, guardrails and raw error text are never
// captured, regardless of switches. No raw request or response is exported.
type TraceConfig struct {
	HideInputs, HideOutputs                                        bool
	HideInputMessages, HideOutputMessages                          bool
	HideInputText, HideOutputText                                  bool
	HideInputImages, HideLLMInvocationParameters, HideErrorDetails bool
}

func TraceConfigFromEnv() TraceConfig {
	b := func(s string) bool { return strings.EqualFold(strings.TrimSpace(os.Getenv("FI_HIDE_"+s)), "true") }
	return TraceConfig{HideInputs: b("INPUTS"), HideOutputs: b("OUTPUTS"), HideInputMessages: b("INPUT_MESSAGES"), HideOutputMessages: b("OUTPUT_MESSAGES"), HideInputText: b("INPUT_TEXT"), HideOutputText: b("OUTPUT_TEXT"), HideInputImages: b("INPUT_IMAGES"), HideLLMInvocationParameters: b("LLM_INVOCATION_PARAMETERS"), HideErrorDetails: b("ERROR_DETAILS")}
}
func (p TraceConfig) input() bool { return !p.HideInputs && !p.HideInputMessages && !p.HideInputText }
func (p TraceConfig) output() bool {
	return !p.HideOutputs && !p.HideOutputMessages && !p.HideOutputText
}
func bounded(s string, n int) string {
	if len(s) <= n {
		return s
	}
	s = s[:n]
	for !utf8.ValidString(s) && len(s) > 0 {
		s = s[:len(s)-1]
	}
	return s
}

// block is an allowlist projection, not an SDK object serialization.
type block struct {
	Type      string `json:"type"`
	Text      string `json:"text,omitempty"`
	ID        string `json:"id,omitempty"`
	Name      string `json:"name,omitempty"`
	Arguments string `json:"arguments,omitempty"`
}
type message struct {
	Role    string  `json:"role"`
	Content []block `json:"content"`
}
type record struct {
	mu                sync.Mutex
	ended             bool
	span              trace.Span
	privacy           TraceConfig
	started           time.Time
	first             bool
	blocks            map[int32]*block
	used              int
	contentAttributes int
	truncated         bool
	usage             tokenUsage
}
type tokenUsage struct{ Input, Output, Total, CacheRead, CacheWrite *int64 }

func int64ptr(v *int32) *int64 {
	if v == nil {
		return nil
	}
	n := int64(*v)
	return &n
}
func (r *record) take(s string) string {
	n := maxCapture - r.used
	if n < 0 {
		n = 0
	}
	if len(s) > n {
		r.truncated = true
	}
	s = bounded(s, n)
	r.used += len(s)
	return s
}
func (r *record) delta(index int32, kind, text string) {
	if text != "" && !r.first {
		r.first = true
		r.span.SetAttributes(attribute.Float64("gen_ai.server.time_to_first_token", float64(time.Since(r.started))/float64(time.Millisecond)))
	}
	if !r.privacy.output() {
		return
	}
	b := r.ensureBlock(index, kind)
	if b == nil {
		return
	}
	text = r.take(text)
	if kind == "tool_use" {
		b.Arguments += text
	} else {
		b.Text += text
	}
}
func (r *record) ensureBlock(index int32, kind string) *block {
	if index < 0 || index >= maxBlocks {
		r.truncated = true
		return nil
	}
	if b := r.blocks[index]; b != nil {
		return b
	}
	b := &block{Type: kind}
	r.blocks[index] = b
	return b
}
func (r *record) finish(err error, closed bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.ended {
		return
	}
	r.ended = true
	if r.privacy.output() && len(r.blocks) > 0 {
		keys := make([]int, 0, len(r.blocks))
		for k := range r.blocks {
			keys = append(keys, int(k))
		}
		sort.Ints(keys)
		blocks := make([]block, 0, len(keys))
		for _, k := range keys {
			blocks = append(blocks, *r.blocks[int32(k)])
		}
		r.messages("output", []message{{Role: "assistant", Content: blocks}})
	}
	if r.privacy.HideOutputs {
		r.span.SetAttributes(attribute.String("output.value", "__REDACTED__"))
	}
	r.tokens()
	if r.truncated {
		r.span.SetAttributes(attribute.Bool("traceai.capture.truncated", true))
	}
	if closed {
		r.span.SetAttributes(attribute.Bool("traceai.stream.closed_early", true))
	}
	if err != nil {
		failure(r.span, err)
	}
	r.span.End()
	// Release captured content even if the customer retains the closed stream.
	r.blocks = nil
}
func (r *record) tokens() {
	for _, x := range []struct {
		k string
		v *int64
	}{{"input_tokens", r.usage.Input}, {"output_tokens", r.usage.Output}, {"total_tokens", r.usage.Total}, {"cache_read_tokens", r.usage.CacheRead}, {"cache_write_tokens", r.usage.CacheWrite}} {
		if x.v != nil && *x.v >= 0 {
			r.span.SetAttributes(attribute.Int64("gen_ai.usage."+x.k, *x.v))
		}
	}
}
func (r *record) setUsage(u *types.TokenUsage) {
	if u == nil {
		return
	}
	r.usage = tokenUsage{Input: int64ptr(u.InputTokens), Output: int64ptr(u.OutputTokens), Total: int64ptr(u.TotalTokens), CacheRead: int64ptr(u.CacheReadInputTokens), CacheWrite: int64ptr(u.CacheWriteInputTokens)}
}
func (r *record) stop(s string) {
	// Stop reasons are machine values, never a second channel for response text.
	switch s {
	case "end_turn", "tool_use", "max_tokens", "stop_sequence", "guardrail_intervened", "content_filtered", "malformed_model_output", "malformed_tool_use", "model_context_window_exceeded", "refusal", "pause_turn":
		r.span.SetAttributes(attribute.StringSlice("gen_ai.response.finish_reasons", []string{s}))
	}
}

// Reserve attribute slots for usage, errors and lifecycle metadata under the
// default OTel span limit. Each side gets at most 40 content attributes.
func (r *record) contentAttrs(values ...attribute.KeyValue) {
	for _, value := range values {
		if r.contentAttributes >= 40 {
			r.truncated = true
			return
		}
		r.span.SetAttributes(value)
		r.contentAttributes++
	}
}
func (r *record) messages(direction string, ms []message) {
	// Keep both Future AGI indexed messages and a bounded summary, derived from
	// the same sanitized projection. The SDK remains the source of application data.
	for i, m := range ms {
		prefix := "gen_ai." + direction + ".messages." + strconv.Itoa(i) + ".message."
		r.contentAttrs(attribute.String(prefix+"role", m.Role))
		var text strings.Builder
		tool := 0
		for _, b := range m.Content {
			switch b.Type {
			case "text", "tool_result":
				text.WriteString(b.Text)
				if b.Type == "tool_result" {
					r.contentAttrs(attribute.String(prefix+"tool_call_id", b.ID))
				}
			case "tool_use":
				key := prefix + "tool_calls." + strconv.Itoa(tool) + ".tool_call."
				r.contentAttrs(attribute.String(key+"id", b.ID), attribute.String(key+"function.name", b.Name), attribute.String(key+"function.arguments", b.Arguments))
				tool++
			}
		}
		if text.Len() > 0 {
			r.contentAttrs(attribute.String(prefix+"content", text.String()))
		}
	}
	data, _ := json.Marshal(ms)
	if len(data) <= maxCapture {
		r.contentAttrs(attribute.String(direction+".value", string(data)), attribute.String(direction+".mime_type", "application/json"))
	} else {
		r.truncated = true
	}
}
func (r *record) project(content []types.ContentBlock) []block {
	out := make([]block, 0)
	for i, v := range content {
		if i >= maxBlocks {
			r.truncated = true
			break
		}
		switch v := v.(type) {
		case *types.ContentBlockMemberText:
			out = append(out, block{Type: "text", Text: r.take(v.Value)})
		case *types.ContentBlockMemberToolUse:
			out = append(out, block{Type: "tool_use", ID: r.take(str(v.Value.ToolUseId)), Name: r.take(str(v.Value.Name)), Arguments: r.doc(v.Value.Input)})
		case *types.ContentBlockMemberToolResult:
			b := block{Type: "tool_result", ID: r.take(str(v.Value.ToolUseId))}
			for j, c := range v.Value.Content {
				if j >= maxBlocks {
					r.truncated = true
					break
				}
				switch c := c.(type) {
				case *types.ToolResultContentBlockMemberText:
					b.Text += r.take(c.Value)
				case *types.ToolResultContentBlockMemberJson:
					b.Text += r.doc(c.Value)
				}
			}
			out = append(out, b)
		}
	}
	return out
}
func str(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
func (r *record) doc(d interface{ MarshalSmithyDocument() ([]byte, error) }) string {
	if d == nil || r.used >= maxCapture {
		return ""
	}
	b, err := d.MarshalSmithyDocument()
	if err != nil {
		r.span.SetAttributes(attribute.Bool("traceai.capture.error", true))
		return ""
	}
	// Don't emit truncated JSON as if it were valid tool arguments.
	if len(b) > maxCapture-r.used {
		r.truncated = true
		return ""
	}
	return r.take(string(b))
}
func (r *record) converseInput(ms []types.Message, sys []types.SystemContentBlock, config *types.InferenceConfiguration, tools *types.ToolConfiguration) {
	if !r.privacy.HideLLMInvocationParameters && config != nil {
		if config.MaxTokens != nil {
			r.span.SetAttributes(attribute.Int64("gen_ai.request.max_tokens", int64(*config.MaxTokens)))
		}
		if config.Temperature != nil {
			r.span.SetAttributes(attribute.Float64("gen_ai.request.temperature", float64(*config.Temperature)))
		}
		if config.TopP != nil {
			r.span.SetAttributes(attribute.Float64("gen_ai.request.top_p", float64(*config.TopP)))
		}
		// Stop sequences are content, so granular input switches cover them too.
		if r.privacy.input() {
			for i, s := range config.StopSequences {
				if i >= 16 {
					break
				}
				r.span.SetAttributes(attribute.String("gen_ai.request.stop_sequences."+strconv.Itoa(i), r.take(s)))
			}
		}
	}
	if r.privacy.HideInputs {
		r.span.SetAttributes(attribute.String("input.value", "__REDACTED__"))
	}
	if !r.privacy.input() {
		return
	}
	projected := make([]message, 0)
	system := message{Role: "system"}
	for i, v := range sys {
		if i >= maxBlocks {
			r.truncated = true
			break
		}
		if v, ok := v.(*types.SystemContentBlockMemberText); ok {
			system.Content = append(system.Content, block{Type: "text", Text: r.take(v.Value)})
		}
	}
	if len(system.Content) > 0 {
		projected = append(projected, system)
	}
	for i, m := range ms {
		if i >= 32 {
			r.truncated = true
			break
		}
		projected = append(projected, message{Role: bounded(string(m.Role), 32), Content: r.project(m.Content)})
	}
	r.messages("input", projected)
	if tools != nil {
		for i, t := range tools.Tools {
			if i >= 16 {
				r.truncated = true
				break
			}
			if t, ok := t.(*types.ToolMemberToolSpec); ok {
				v := struct {
					Name        string          `json:"name"`
					Description string          `json:"description,omitempty"`
					Schema      json.RawMessage `json:"input_schema,omitempty"`
				}{Name: r.take(str(t.Value.Name)), Description: r.take(str(t.Value.Description))}
				if s, ok := t.Value.InputSchema.(*types.ToolInputSchemaMemberJson); ok {
					v.Schema = json.RawMessage(r.doc(s.Value))
				}
				b, _ := json.Marshal(v)
				r.contentAttrs(attribute.String("gen_ai.tool.definitions."+strconv.Itoa(i)+".tool.json_schema", bounded(string(b), maxCapture)))
			}
		}
	}
	r.used = 0 // Separate bounded budgets for input and output.
	r.contentAttributes = 0
}
func (r *record) converseOutput(out *bedrockruntime.ConverseOutput) {
	if r.privacy.output() {
		if m, ok := out.Output.(*types.ConverseOutputMemberMessage); ok {
			for i, b := range r.project(m.Value.Content) {
				copy := b
				r.blocks[int32(i)] = &copy
			}
		}
	}
	r.setUsage(out.Usage)
	r.stop(string(out.StopReason))
}
func (r *record) converseEvent(ev types.ConverseStreamOutput) {
	switch v := ev.(type) {
	case *types.ConverseStreamOutputMemberContentBlockStart:
		if !r.privacy.output() {
			return
		}
		if start, ok := v.Value.Start.(*types.ContentBlockStartMemberToolUse); ok && v.Value.ContentBlockIndex != nil {
			if b := r.ensureBlock(*v.Value.ContentBlockIndex, "tool_use"); b != nil {
				b.ID = r.take(str(start.Value.ToolUseId))
				b.Name = r.take(str(start.Value.Name))
			}
		}
	case *types.ConverseStreamOutputMemberContentBlockDelta:
		if v.Value.ContentBlockIndex == nil {
			return
		}
		switch d := v.Value.Delta.(type) {
		case *types.ContentBlockDeltaMemberText:
			r.delta(*v.Value.ContentBlockIndex, "text", d.Value)
		case *types.ContentBlockDeltaMemberToolUse:
			r.delta(*v.Value.ContentBlockIndex, "tool_use", str(d.Value.Input))
		}
	case *types.ConverseStreamOutputMemberMessageStop:
		r.stop(string(v.Value.StopReason))
	case *types.ConverseStreamOutputMemberMetadata:
		r.setUsage(v.Value.Usage)
	}
}
