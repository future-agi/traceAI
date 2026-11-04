package traceaianthropic

import (
	"bytes"
	"encoding/json"
	"strings"
	"unicode/utf8"
)

// maxValueBytes caps input.value and output.value. A longer value is cut and
// the span gets anthropic.content.truncated=true.
const maxValueBytes = 750_000

// messagesRequest is what the span reads from a Messages request body.
type messagesRequest struct {
	Model         string          `json:"model"`
	MaxTokens     *int64          `json:"max_tokens"`
	Temperature   *float64        `json:"temperature"`
	TopP          *float64        `json:"top_p"`
	TopK          *int64          `json:"top_k"`
	Stream        bool            `json:"stream"`
	StopSequences []string        `json:"stop_sequences"`
	System        json.RawMessage `json:"system"`
	Messages      []messageParam  `json:"messages"`
	Tools         json.RawMessage `json:"tools"`
}

type messageParam struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}

// invocationParameterKeys are the request fields that go into
// gen_ai.request.parameters. They are settings, not content. Fields such as
// mcp_servers, which can hold credentials, and metadata are never copied.
var invocationParameterKeys = []string{"max_tokens", "temperature", "top_p", "top_k", "stream", "tool_choice", "thinking", "service_tier"}

// message is what the span reads from a Message: the body of a non-streaming
// response, or the message that a stream assembles.
type message struct {
	ID           string            `json:"id,omitempty"`
	Type         string            `json:"type,omitempty"`
	Role         string            `json:"role,omitempty"`
	Model        string            `json:"model,omitempty"`
	Content      []json.RawMessage `json:"content"`
	StopReason   *string           `json:"stop_reason"`
	StopSequence *string           `json:"stop_sequence"`
	Usage        *usage            `json:"usage,omitempty"`
}

type usage struct {
	InputTokens              *int64 `json:"input_tokens,omitempty"`
	OutputTokens             *int64 `json:"output_tokens,omitempty"`
	CacheCreationInputTokens *int64 `json:"cache_creation_input_tokens,omitempty"`
	CacheReadInputTokens     *int64 `json:"cache_read_input_tokens,omitempty"`
}

// merge copies every count that o has onto u. A stream's message_delta carries
// the final counts.
func (u *usage) merge(o *usage) {
	if o == nil {
		return
	}
	if o.InputTokens != nil {
		u.InputTokens = o.InputTokens
	}
	if o.OutputTokens != nil {
		u.OutputTokens = o.OutputTokens
	}
	if o.CacheCreationInputTokens != nil {
		u.CacheCreationInputTokens = o.CacheCreationInputTokens
	}
	if o.CacheReadInputTokens != nil {
		u.CacheReadInputTokens = o.CacheReadInputTokens
	}
}

// contentBlock is one content block of a request message or a response.
type contentBlock struct {
	Type      string          `json:"type"`
	Text      string          `json:"text"`
	Thinking  string          `json:"thinking"`
	ID        string          `json:"id"`
	Name      string          `json:"name"`
	Input     json.RawMessage `json:"input"`
	ToolUseID string          `json:"tool_use_id"`
	Content   json.RawMessage `json:"content"`
}

// part is one message part in the OpenTelemetry GenAI JSON shape, spelled the
// way anthropic-sdk-go spells it on its own spans in content mode.
type part struct {
	Type      string          `json:"type"`
	Content   *string         `json:"content,omitempty"`
	ID        string          `json:"id,omitempty"`
	Name      string          `json:"name,omitempty"`
	Arguments json.RawMessage `json:"arguments,omitempty"`
	Response  any             `json:"response,omitempty"`
}

type chatMessage struct {
	Role         string `json:"role"`
	Parts        []part `json:"parts"`
	FinishReason string `json:"finish_reason,omitempty"`
}

type textBlock struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

type typeOnly struct {
	Type string `json:"type"`
}

func strPtr(s string) *string { return &s }

// blockParts returns the parts of a content field: a string or a list of blocks.
// hideText replaces every text with the redaction sentinel; hideImages leaves
// image and document parts out.
func blockParts(raw json.RawMessage, hideText, hideImages bool) []part {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return nil
	}
	if raw[0] == '"' {
		var s string
		if json.Unmarshal(raw, &s) != nil {
			return nil
		}
		return []part{textPart(s, hideText)}
	}
	var blocks []contentBlock
	if json.Unmarshal(raw, &blocks) != nil {
		return nil
	}
	parts := make([]part, 0, len(blocks))
	for _, b := range blocks {
		if p, ok := blockPart(b, hideText, hideImages); ok {
			parts = append(parts, p)
		}
	}
	return parts
}

func textPart(s string, hide bool) part {
	if hide {
		s = redactedValue
	}
	return part{Type: "text", Content: strPtr(s)}
}

func blockPart(b contentBlock, hideText, hideImages bool) (part, bool) {
	switch b.Type {
	case "text":
		return textPart(b.Text, hideText), true
	case "thinking":
		s := b.Thinking
		if hideText {
			s = redactedValue
		}
		return part{Type: "reasoning", Content: strPtr(s)}, true
	case "redacted_thinking":
		return part{Type: "reasoning"}, true
	case "tool_use", "server_tool_use", "mcp_tool_use":
		args := b.Input
		if len(bytes.TrimSpace(args)) == 0 {
			args = json.RawMessage("{}")
		}
		if hideText {
			args = json.RawMessage(`"` + redactedValue + `"`)
		}
		return part{Type: "tool_call", ID: b.ID, Name: b.Name, Arguments: args}, true
	case "tool_result":
		return part{Type: "tool_call_response", ID: b.ToolUseID, Response: toolResultResponse(b.Content, hideText)}, true
	case "image", "document":
		if hideImages {
			return part{}, false
		}
	}
	if strings.HasSuffix(b.Type, "_tool_result") {
		return part{Type: "tool_call_response", ID: b.ToolUseID, Response: toolResultResponse(b.Content, hideText)}, true
	}
	// Image and document bytes, and blocks this package does not know, keep
	// their type only, as the SDK's own spans do.
	return part{Type: "anthropic_" + b.Type}, true
}

// toolResultResponse returns the response of a tool result: its text blocks,
// and the type of any other block.
func toolResultResponse(raw json.RawMessage, hideText bool) []any {
	raw = bytes.TrimSpace(raw)
	out := []any{}
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return out
	}
	if raw[0] == '"' {
		var s string
		if json.Unmarshal(raw, &s) == nil {
			if hideText {
				s = redactedValue
			}
			out = append(out, textBlock{Type: "text", Text: s})
		}
		return out
	}
	var blocks []contentBlock
	if json.Unmarshal(raw, &blocks) != nil {
		return out
	}
	for _, b := range blocks {
		if b.Type == "text" {
			t := b.Text
			if hideText {
				t = redactedValue
			}
			out = append(out, textBlock{Type: "text", Text: t})
			continue
		}
		out = append(out, typeOnly{Type: b.Type})
	}
	return out
}

// inputMessages returns gen_ai.input.messages for the request's messages.
func inputMessages(req *messagesRequest, cfg TraceConfig) []chatMessage {
	msgs := make([]chatMessage, 0, len(req.Messages))
	for _, m := range req.Messages {
		msgs = append(msgs, chatMessage{Role: m.Role, Parts: nonNil(blockParts(m.Content, cfg.HideInputText, cfg.HideInputImages))})
	}
	return msgs
}

// outputMessages returns gen_ai.output.messages for a response message.
func outputMessages(m *message, cfg TraceConfig) []chatMessage {
	var parts []part
	for _, raw := range m.Content {
		parts = append(parts, blockParts(json.RawMessage("["+string(raw)+"]"), cfg.HideOutputText, false)...)
	}
	role := m.Role
	if role == "" {
		role = "assistant"
	}
	out := chatMessage{Role: role, Parts: nonNil(parts)}
	if m.StopReason != nil {
		out.FinishReason = *m.StopReason
	}
	return []chatMessage{out}
}

func nonNil(p []part) []part {
	if p == nil {
		return []part{}
	}
	return p
}

// invocationParameters returns gen_ai.request.parameters: the settings of the
// request, without its content.
func invocationParameters(body map[string]json.RawMessage) (string, bool) {
	params := map[string]json.RawMessage{}
	for _, k := range invocationParameterKeys {
		if v, ok := body[k]; ok {
			params[k] = v
		}
	}
	if len(params) == 0 {
		return "", false
	}
	b, err := json.Marshal(params)
	return string(b), err == nil
}

// sanitizeInputValue returns the request body for input.value. Credentials
// that a request can carry (the authorization_token of an MCP server) are
// always replaced with the redaction sentinel, and so is base64 image and
// document data longer than the configured maximum, or any image data when
// images are hidden. A body that is not JSON is used as it is.
func sanitizeInputValue(body []byte, cfg TraceConfig) string {
	var doc any
	if json.Unmarshal(body, &doc) != nil {
		return string(body)
	}
	if !redactInput(doc, cfg.imageMax(), cfg.HideInputImages) {
		return string(body)
	}
	b, err := json.Marshal(doc)
	if err != nil {
		return string(body)
	}
	return string(b)
}

// credentialKeys are request fields whose value is a credential. They are
// never exported, whatever the TraceConfig says.
var credentialKeys = []string{"authorization_token"}

// redactInput walks a decoded JSON value and replaces every credential, and
// the data of every {"type":"base64","data":...} source that is too long or
// that belongs to an image when images are hidden. It reports whether it
// changed anything.
func redactInput(v any, max int, hideImages bool) bool {
	changed := false
	switch t := v.(type) {
	case map[string]any:
		for _, k := range credentialKeys {
			if _, ok := t[k]; ok {
				t[k] = redactedValue
				changed = true
			}
		}
		if blockType, _ := t["type"].(string); blockType == "image" && hideImages {
			if src, ok := t["source"].(map[string]any); ok {
				for _, k := range []string{"data", "url"} {
					if _, ok := src[k]; ok {
						src[k] = redactedValue
						changed = true
					}
				}
			}
		}
		if st, _ := t["type"].(string); st == "base64" {
			if data, ok := t["data"].(string); ok && len(data) > max {
				t["data"] = redactedValue
				changed = true
			}
		}
		for _, child := range t {
			if redactInput(child, max, hideImages) {
				changed = true
			}
		}
	case []any:
		for _, child := range t {
			if redactInput(child, max, hideImages) {
				changed = true
			}
		}
	}
	return changed
}

// systemInstructions returns gen_ai.system_instructions for the request's
// system field: a string or a list of text blocks.
func systemInstructions(raw json.RawMessage, cfg TraceConfig) []part {
	return blockParts(raw, cfg.HideInputText, cfg.HideInputImages)
}

func marshalString(v any) (string, bool) {
	b, err := json.Marshal(v)
	if err != nil {
		return "", false
	}
	return string(b), true
}

// capValue cuts s to at most maxValueBytes, at a character boundary, and
// reports whether it did.
func capValue(s string) (string, bool) {
	if len(s) <= maxValueBytes {
		return s, false
	}
	i := maxValueBytes
	for i > 0 && !utf8.RuneStart(s[i]) {
		i--
	}
	return s[:i], true
}
