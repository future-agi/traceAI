package traceaiopenai

import (
	"encoding/json"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel/attribute"
)

type parsedRequest struct {
	model string
	attrs []attribute.KeyValue
}

type toolCall struct {
	Index    *int   `json:"index,omitempty"`
	ID       string `json:"id,omitempty"`
	Type     string `json:"type,omitempty"`
	Function struct {
		Name      string `json:"name,omitempty"`
		Arguments string `json:"arguments,omitempty"`
	} `json:"function"`
}

type inMessage struct {
	Role       string          `json:"role"`
	Content    json.RawMessage `json:"content"`
	Name       string          `json:"name"`
	ToolCallID string          `json:"tool_call_id"`
	ToolCalls  []toolCall      `json:"tool_calls"`
}

type contentPart struct {
	Type     string `json:"type"`
	Text     string `json:"text"`
	ImageURL *struct {
		URL    string `json:"url"`
		Detail string `json:"detail,omitempty"`
	} `json:"image_url"`
}

// invocationParams are the request fields copied into gen_ai.request.parameters.
// It is an allow list: free-form fields (user, safety_identifier, metadata,
// prompt_cache_key, prediction, audio) are never copied.
var invocationParams = []string{
	"frequency_penalty", "logprobs", "max_completion_tokens", "max_tokens", "modalities", "n",
	"parallel_tool_calls", "presence_penalty", "reasoning_effort", "seed", "service_tier", "stop",
	"store", "stream", "stream_options", "temperature", "tool_choice", "top_logprobs", "top_p", "verbosity",
}

func parseChatRequest(body []byte, tc TraceConfig) parsedRequest {
	var p parsedRequest
	if len(body) == 0 {
		return p
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return p
	}
	if s, ok := jsonString(raw["model"]); ok && s != "" {
		p.model = s
		p.attrs = append(p.attrs, attribute.String(GenAIRequestModel, s))
	}
	if f, ok := jsonFloat(raw["temperature"]); ok {
		p.attrs = append(p.attrs, attribute.Float64(GenAIRequestTemperature, f))
	}
	if f, ok := jsonFloat(raw["top_p"]); ok {
		p.attrs = append(p.attrs, attribute.Float64(GenAIRequestTopP, f))
	}
	if n, ok := jsonInt(raw["max_completion_tokens"]); ok {
		p.attrs = append(p.attrs, attribute.Int64(GenAIRequestMaxTokens, n))
	} else if n, ok := jsonInt(raw["max_tokens"]); ok {
		p.attrs = append(p.attrs, attribute.Int64(GenAIRequestMaxTokens, n))
	}
	if f, ok := jsonFloat(raw["frequency_penalty"]); ok {
		p.attrs = append(p.attrs, attribute.Float64(GenAIRequestFrequencyPenalty, f))
	}
	if f, ok := jsonFloat(raw["presence_penalty"]); ok {
		p.attrs = append(p.attrs, attribute.Float64(GenAIRequestPresencePenalty, f))
	}
	if n, ok := jsonInt(raw["seed"]); ok {
		p.attrs = append(p.attrs, attribute.Int64(GenAIRequestSeed, n))
	}
	if stops := jsonStrings(raw["stop"]); len(stops) > 0 {
		p.attrs = append(p.attrs, attribute.StringSlice(GenAIRequestStopSequences, stops))
	}

	params := map[string]json.RawMessage{}
	for _, k := range invocationParams {
		if v, ok := raw[k]; ok && string(v) != "null" {
			params[k] = v
		}
	}
	if rf, ok := raw["response_format"]; ok {
		var t struct {
			Type string `json:"type"`
		}
		if json.Unmarshal(rf, &t) == nil && t.Type != "" {
			params["response_format"], _ = json.Marshal(map[string]string{"type": t.Type})
		}
	}
	if len(params) > 0 {
		if b, err := json.Marshal(params); err == nil {
			p.attrs = append(p.attrs, attribute.String(GenAIRequestParameters, string(b)))
		}
	}

	var tools []json.RawMessage
	if json.Unmarshal(raw["tools"], &tools) == nil {
		for i, t := range tools {
			p.attrs = append(p.attrs, attribute.String(
				GenAIToolDefinitions+"."+strconv.Itoa(i)+"."+ToolJSONSchema, compactJSON(t)))
		}
	}

	var msgs []inMessage
	if json.Unmarshal(raw["messages"], &msgs) == nil && len(msgs) > 0 {
		p.attrs = append(p.attrs, inputMessageAttributes(msgs)...)
		if v := inputValue(msgs, tc); v != "" {
			p.attrs = append(p.attrs,
				attribute.String(InputValue, v),
				attribute.String(InputMimeType, MimeTypeJSON))
		}
	}
	return p
}

// inputMessageAttributes flattens the request messages into the traceAI
// gen_ai.input.messages.{i}.message.* keys.
func inputMessageAttributes(msgs []inMessage) []attribute.KeyValue {
	var kvs []attribute.KeyValue
	for i, m := range msgs {
		prefix := GenAIInputMessages + "." + strconv.Itoa(i) + "."
		if m.Role != "" {
			kvs = append(kvs, attribute.String(prefix+MessageRole, m.Role))
		}
		if m.Name != "" {
			kvs = append(kvs, attribute.String(prefix+MessageName, m.Name))
		}
		if m.ToolCallID != "" {
			kvs = append(kvs, attribute.String(prefix+MessageToolCallID, m.ToolCallID))
		}
		if s, ok := jsonString(m.Content); ok {
			if s != "" {
				kvs = append(kvs, attribute.String(prefix+MessageContent, s))
			}
		} else {
			var parts []contentPart
			if json.Unmarshal(m.Content, &parts) == nil {
				for k, part := range parts {
					pp := prefix + MessageContents + "." + strconv.Itoa(k) + "."
					switch part.Type {
					case "text":
						kvs = append(kvs, attribute.String(pp+MessageContentType, "text"))
						if part.Text != "" {
							kvs = append(kvs, attribute.String(pp+MessageContentText, part.Text))
						}
					case "image_url":
						kvs = append(kvs, attribute.String(pp+MessageContentType, "image"))
						if part.ImageURL != nil && part.ImageURL.URL != "" {
							kvs = append(kvs, attribute.String(pp+MessageContentImage, part.ImageURL.URL))
						}
					case "input_audio":
						// Audio bytes are never copied.
						kvs = append(kvs, attribute.String(pp+MessageContentType, "audio"))
					case "":
					default:
						kvs = append(kvs, attribute.String(pp+MessageContentType, part.Type))
					}
				}
			}
		}
		kvs = append(kvs, toolCallAttributes(prefix, m.ToolCalls)...)
	}
	return kvs
}

func toolCallAttributes(prefix string, calls []toolCall) []attribute.KeyValue {
	var kvs []attribute.KeyValue
	for j, tcall := range calls {
		tp := prefix + MessageToolCalls + "." + strconv.Itoa(j) + "."
		if tcall.ID != "" {
			kvs = append(kvs, attribute.String(tp+ToolCallID, tcall.ID))
		}
		if tcall.Function.Name != "" {
			kvs = append(kvs, attribute.String(tp+ToolCallFunctionName, tcall.Function.Name))
		}
		if tcall.Function.Arguments != "" {
			kvs = append(kvs, attribute.String(tp+ToolCallFunctionArgumentsJSON, tcall.Function.Arguments))
		}
	}
	return kvs
}

// inputValue is the conversation as JSON for input.value: role, text, image
// URLs (long base64 images redacted), tool calls and tool results. Audio and
// file bytes are left out.
func inputValue(msgs []inMessage, tc TraceConfig) string {
	imageMax := tc.Base64ImageMaxLength
	if imageMax <= 0 {
		imageMax = DefaultBase64ImageMaxLength
	}
	out := make([]map[string]any, 0, len(msgs))
	for _, m := range msgs {
		o := map[string]any{"role": m.Role}
		if m.Name != "" {
			o["name"] = m.Name
		}
		if m.ToolCallID != "" {
			o["tool_call_id"] = m.ToolCallID
		}
		if s, ok := jsonString(m.Content); ok {
			o["content"] = s
		} else {
			var parts []contentPart
			if json.Unmarshal(m.Content, &parts) == nil && len(parts) > 0 {
				simple := make([]map[string]any, 0, len(parts))
				for _, part := range parts {
					switch part.Type {
					case "text":
						simple = append(simple, map[string]any{"type": "text", "text": part.Text})
					case "image_url":
						url := ""
						if part.ImageURL != nil {
							url = part.ImageURL.URL
						}
						if strings.HasPrefix(url, "data:") && len(url) > imageMax {
							url = RedactedValue
						}
						simple = append(simple, map[string]any{"type": "image_url", "image_url": map[string]string{"url": url}})
					default:
						simple = append(simple, map[string]any{"type": part.Type})
					}
				}
				o["content"] = simple
			}
		}
		if len(m.ToolCalls) > 0 {
			o["tool_calls"] = simpleToolCalls(m.ToolCalls)
		}
		out = append(out, o)
	}
	b, err := json.Marshal(out)
	if err != nil {
		return ""
	}
	return string(b)
}

func simpleToolCalls(calls []toolCall) []map[string]string {
	out := make([]map[string]string, 0, len(calls))
	for _, c := range calls {
		out = append(out, map[string]string{"id": c.ID, "name": c.Function.Name, "arguments": c.Function.Arguments})
	}
	return out
}

func compactJSON(b json.RawMessage) string {
	var v any
	if json.Unmarshal(b, &v) != nil {
		return string(b)
	}
	out, err := json.Marshal(v)
	if err != nil {
		return string(b)
	}
	return string(out)
}

func jsonString(b json.RawMessage) (string, bool) {
	if len(b) == 0 || b[0] != '"' {
		return "", false
	}
	var s string
	if json.Unmarshal(b, &s) != nil {
		return "", false
	}
	return s, true
}

func jsonFloat(b json.RawMessage) (float64, bool) {
	if len(b) == 0 || string(b) == "null" {
		return 0, false
	}
	var f float64
	if json.Unmarshal(b, &f) != nil {
		return 0, false
	}
	return f, true
}

func jsonInt(b json.RawMessage) (int64, bool) {
	if len(b) == 0 || string(b) == "null" {
		return 0, false
	}
	var n int64
	if json.Unmarshal(b, &n) != nil {
		return 0, false
	}
	return n, true
}

func jsonStrings(b json.RawMessage) []string {
	if s, ok := jsonString(b); ok {
		if s == "" {
			return nil
		}
		return []string{s}
	}
	var ss []string
	if json.Unmarshal(b, &ss) != nil {
		return nil
	}
	return ss
}
