package traceaiopenai

import (
	"encoding/json"
	"net/http"
	"strings"

	"go.opentelemetry.io/otel/attribute"
)

type operation string

const (
	opChat       operation = "chat"
	opResponses  operation = "generate_content"
	opEmbeddings operation = "embeddings"
)

func operationForRequest(req *http.Request) operation {
	if req == nil || req.URL == nil || req.Method != http.MethodPost {
		return ""
	}
	path := strings.TrimRight(req.URL.Path, "/")
	switch {
	case strings.HasSuffix(path, "/chat/completions"):
		return opChat
	case strings.HasSuffix(path, "/responses"):
		return opResponses
	case strings.HasSuffix(path, "/embeddings"):
		return opEmbeddings
	default:
		return ""
	}
}

// parseRequest normalizes supported Responses input items into the existing
// message representation so content masking is identical to Chat Completions.
// It never forwards the raw request into telemetry (files, encrypted reasoning,
// metadata, built-in tool configuration and arbitrary future items stay out).
func parseRequest(body []byte, tc TraceConfig, op operation) parsedRequest {
	if op == opChat {
		return parseChatRequest(body, tc)
	}
	var raw map[string]json.RawMessage
	if json.Unmarshal(body, &raw) != nil {
		return parsedRequest{}
	}
	if op == opEmbeddings {
		p := parsedRequest{}
		if model, ok := jsonString(raw["model"]); ok {
			p.model = model
			p.attrs = append(p.attrs, attribute.String(GenAIRequestModel, model), attribute.String("embedding.model_name", model))
		}
		// Strings and token ID arrays are accepted by the SDK. Vectors returned
		// by the API are deliberately not recorded; they are large and sensitive.
		if input := raw["input"]; len(input) > 0 && string(input) != "null" {
			p.attrs = append(p.attrs, attribute.String(InputValue, compactJSON(input)), attribute.String(InputMimeType, MimeTypeJSON))
		}
		return p
	}

	normalized := map[string]any{}
	for _, key := range []string{"model", "temperature", "top_p", "parallel_tool_calls", "store", "stream", "service_tier"} {
		if value, ok := raw[key]; ok {
			normalized[key] = value
		}
	}
	// Only function definitions, not hosted-tool config (which can contain
	// credentials or user location), belong in model tool-schema attributes.
	var tools []struct {
		Type        string          `json:"type"`
		Name        string          `json:"name"`
		Description string          `json:"description,omitempty"`
		Parameters  json.RawMessage `json:"parameters,omitempty"`
		Strict      *bool           `json:"strict,omitempty"`
	}
	if !tc.HideInputs && json.Unmarshal(raw["tools"], &tools) == nil {
		var functions []any
		for _, tool := range tools {
			if tool.Type == "function" {
				functions = append(functions, tool)
			}
		}
		normalized["tools"] = functions
	}
	var msgs []inMessage
	if instructions, ok := jsonString(raw["instructions"]); ok && instructions != "" {
		msgs = append(msgs, inMessage{Role: "system", Content: raw["instructions"]})
	}
	if _, ok := jsonString(raw["input"]); ok {
		msgs = append(msgs, inMessage{Role: "user", Content: raw["input"]})
	} else {
		var items []responseItem
		if json.Unmarshal(raw["input"], &items) == nil {
			for _, item := range items {
				if msg, ok := item.inputMessage(); ok {
					msgs = append(msgs, msg)
				}
			}
		}
	}
	normalized["messages"] = msgs
	b, _ := json.Marshal(normalized)
	p := parseChatRequest(b, tc)
	if n, ok := jsonInt(raw["max_output_tokens"]); ok {
		p.attrs = append(p.attrs, attribute.Int64(GenAIRequestMaxTokens, n))
	}
	return p
}

type responseItem struct {
	Type      string          `json:"type"`
	Role      string          `json:"role"`
	Content   json.RawMessage `json:"content"`
	CallID    string          `json:"call_id"`
	Name      string          `json:"name"`
	Arguments string          `json:"arguments"`
	Output    json.RawMessage `json:"output"`
}

func (item responseItem) call() toolCall {
	var call toolCall
	call.ID, call.Type, call.Function.Name, call.Function.Arguments = item.CallID, "function", item.Name, item.Arguments
	return call
}

func (item responseItem) inputMessage() (inMessage, bool) {
	switch item.Type {
	case "", "message":
		if item.Role == "" {
			return inMessage{}, false
		}
		return inMessage{Role: item.Role, Content: normalizeResponseContent(item.Content)}, true
	case "function_call":
		return inMessage{Role: "assistant", ToolCalls: []toolCall{item.call()}}, true
	case "function_call_output":
		return inMessage{Role: "tool", ToolCallID: item.CallID, Content: normalizeResponseContent(item.Output)}, true
	default:
		return inMessage{}, false
	}
}

func normalizeResponseContent(raw json.RawMessage) json.RawMessage {
	if _, ok := jsonString(raw); ok {
		return raw
	}
	var parts []struct {
		Type     string `json:"type"`
		Text     string `json:"text"`
		Refusal  string `json:"refusal"`
		ImageURL string `json:"image_url"`
	}
	if json.Unmarshal(raw, &parts) != nil {
		return nil
	}
	out := make([]contentPart, 0, len(parts))
	for _, part := range parts {
		switch part.Type {
		case "input_text", "output_text":
			out = append(out, contentPart{Type: "text", Text: part.Text})
		case "refusal":
			out = append(out, contentPart{Type: "text", Text: part.Refusal})
		case "input_image":
			p := contentPart{Type: "image_url"}
			p.ImageURL = &struct {
				URL    string `json:"url"`
				Detail string `json:"detail,omitempty"`
			}{URL: part.ImageURL}
			out = append(out, p)
		}
	}
	b, _ := json.Marshal(out)
	return b
}
