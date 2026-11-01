package traceaigenai

import (
	"encoding/json"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel/attribute"
)

// part is one Gemini REST Part. Only the fields this package reads are
// declared; thoughtSignature, videoMetadata and the like are never copied.
type part struct {
	Text       *string `json:"text"`
	Thought    bool    `json:"thought"`
	InlineData *struct {
		MimeType string `json:"mimeType"`
		Data     string `json:"data"`
	} `json:"inlineData"`
	FileData *struct {
		MimeType string `json:"mimeType"`
		FileURI  string `json:"fileUri"`
	} `json:"fileData"`
	FunctionCall *struct {
		ID   string          `json:"id"`
		Name string          `json:"name"`
		Args json.RawMessage `json:"args"`
	} `json:"functionCall"`
	FunctionResponse *struct {
		ID       string          `json:"id"`
		Name     string          `json:"name"`
		Response json.RawMessage `json:"response"`
	} `json:"functionResponse"`
	ExecutableCode      json.RawMessage `json:"executableCode"`
	CodeExecutionResult json.RawMessage `json:"codeExecutionResult"`
}

// content is one Gemini REST Content (a turn).
type content struct {
	Role  string `json:"role"`
	Parts []part `json:"parts"`
}

type generateRequest struct {
	Contents          []content       `json:"contents"`
	SystemInstruction *content        `json:"systemInstruction"`
	Tools             []tool          `json:"tools"`
	GenerationConfig  json.RawMessage `json:"generationConfig"`
}

type tool struct {
	FunctionDeclarations []struct {
		Name                 string          `json:"name"`
		Description          string          `json:"description"`
		Parameters           json.RawMessage `json:"parameters"`
		ParametersJSONSchema json.RawMessage `json:"parametersJsonSchema"`
	} `json:"functionDeclarations"`
}

type parsedRequest struct {
	attrs []attribute.KeyValue
}

// invocationParams are the generationConfig fields copied into
// gen_ai.request.parameters. It is an allow list: response schemas, speech
// and routing config are not copied.
var invocationParams = []string{
	"candidateCount", "frequencyPenalty", "logprobs", "maxOutputTokens", "mediaResolution", "presencePenalty",
	"responseLogprobs", "responseMimeType", "responseModalities", "seed", "stopSequences", "temperature",
	"thinkingConfig", "topK", "topP",
}

// parseGenerateRequest reads the Gemini REST GenerateContentRequest body the
// SDK sends ({contents, systemInstruction, tools, generationConfig, ...}).
func parseGenerateRequest(body []byte, tc TraceConfig) parsedRequest {
	var p parsedRequest
	if len(body) == 0 {
		return p
	}
	var r generateRequest
	if err := json.Unmarshal(body, &r); err != nil {
		return p
	}

	if len(r.GenerationConfig) > 0 {
		var raw map[string]json.RawMessage
		if json.Unmarshal(r.GenerationConfig, &raw) == nil {
			p.attrs = append(p.attrs, generationConfigAttributes(raw)...)
		}
	}

	n := 0
	for _, t := range r.Tools {
		for _, fd := range t.FunctionDeclarations {
			def := map[string]any{"name": fd.Name}
			if fd.Description != "" {
				def["description"] = fd.Description
			}
			switch {
			case len(fd.Parameters) > 0 && string(fd.Parameters) != "null":
				def["parameters"] = json.RawMessage(fd.Parameters)
			case len(fd.ParametersJSONSchema) > 0 && string(fd.ParametersJSONSchema) != "null":
				def["parameters"] = json.RawMessage(fd.ParametersJSONSchema)
			}
			if b, err := json.Marshal(def); err == nil {
				p.attrs = append(p.attrs, attribute.String(
					GenAIToolDefinitions+"."+strconv.Itoa(n)+"."+ToolJSONSchema, string(b)))
				n++
			}
		}
	}

	var msgs []content
	if r.SystemInstruction != nil && len(r.SystemInstruction.Parts) > 0 {
		sys := *r.SystemInstruction
		sys.Role = "system"
		msgs = append(msgs, sys)
	}
	msgs = append(msgs, r.Contents...)
	for i, m := range msgs {
		p.attrs = append(p.attrs, messageAttributes(GenAIInputMessages+"."+strconv.Itoa(i)+".", m, "user", false)...)
	}
	if v := conversationJSON(msgs, "user", tc, false); v != "" {
		p.attrs = append(p.attrs,
			attribute.String(InputValue, v),
			attribute.String(InputMimeType, MimeTypeJSON))
	}
	return p
}

func generationConfigAttributes(raw map[string]json.RawMessage) []attribute.KeyValue {
	var kvs []attribute.KeyValue
	if f, ok := jsonFloat(raw["temperature"]); ok {
		kvs = append(kvs, attribute.Float64(GenAIRequestTemperature, f))
	}
	if f, ok := jsonFloat(raw["topP"]); ok {
		kvs = append(kvs, attribute.Float64(GenAIRequestTopP, f))
	}
	if f, ok := jsonFloat(raw["topK"]); ok {
		kvs = append(kvs, attribute.Float64(GenAIRequestTopK, f))
	}
	if n, ok := jsonInt(raw["maxOutputTokens"]); ok {
		kvs = append(kvs, attribute.Int64(GenAIRequestMaxTokens, n))
	}
	if f, ok := jsonFloat(raw["frequencyPenalty"]); ok {
		kvs = append(kvs, attribute.Float64(GenAIRequestFrequencyPenalty, f))
	}
	if f, ok := jsonFloat(raw["presencePenalty"]); ok {
		kvs = append(kvs, attribute.Float64(GenAIRequestPresencePenalty, f))
	}
	if n, ok := jsonInt(raw["seed"]); ok {
		kvs = append(kvs, attribute.Int64(GenAIRequestSeed, n))
	}
	if stops := jsonStrings(raw["stopSequences"]); len(stops) > 0 {
		kvs = append(kvs, attribute.StringSlice(GenAIRequestStopSequences, stops))
	}
	params := map[string]json.RawMessage{}
	for _, k := range invocationParams {
		if v, ok := raw[k]; ok && string(v) != "null" {
			params[k] = v
		}
	}
	if len(params) > 0 {
		if b, err := json.Marshal(params); err == nil {
			kvs = append(kvs, attribute.String(GenAIRequestParameters, string(b)))
		}
	}
	return kvs
}

// partsView is one turn split into what traceAI records.
type partsView struct {
	items   []contentItem // text and media, in part order
	calls   []functionCall
	results []functionResult
}

type contentItem struct {
	kind  string // "text", "image", "audio", "video", "file", "code"
	text  string
	image string // data: URL or file URI, images only
}

type functionCall struct {
	id, name, args string
}

type functionResult struct {
	id, name, response string
}

func viewParts(parts []part) partsView {
	var v partsView
	for _, pt := range parts {
		switch {
		case pt.FunctionCall != nil:
			v.calls = append(v.calls, functionCall{
				id: pt.FunctionCall.ID, name: pt.FunctionCall.Name, args: compactJSON(pt.FunctionCall.Args),
			})
		case pt.FunctionResponse != nil:
			v.results = append(v.results, functionResult{
				id: pt.FunctionResponse.ID, name: pt.FunctionResponse.Name, response: compactJSON(pt.FunctionResponse.Response),
			})
		case pt.Text != nil:
			// Thought summaries are reasoning, not the answer.
			if !pt.Thought && *pt.Text != "" {
				v.items = append(v.items, contentItem{kind: "text", text: *pt.Text})
			}
		case pt.InlineData != nil:
			kind := mediaKind(pt.InlineData.MimeType)
			it := contentItem{kind: kind}
			// Image bytes are kept as a data: URL (subject to
			// FI_HIDE_INPUT_IMAGES and FI_BASE64_IMAGE_MAX_LENGTH). Audio,
			// video and file bytes are never copied.
			if kind == "image" && pt.InlineData.Data != "" {
				it.image = "data:" + pt.InlineData.MimeType + ";base64," + pt.InlineData.Data
			}
			v.items = append(v.items, it)
		case pt.FileData != nil:
			kind := mediaKind(pt.FileData.MimeType)
			it := contentItem{kind: kind}
			if kind == "image" {
				it.image = pt.FileData.FileURI
			}
			v.items = append(v.items, it)
		case len(pt.ExecutableCode) > 0 || len(pt.CodeExecutionResult) > 0:
			v.items = append(v.items, contentItem{kind: "code"})
		}
	}
	return v
}

func mediaKind(mime string) string {
	switch {
	case strings.HasPrefix(mime, "image/"):
		return "image"
	case strings.HasPrefix(mime, "audio/"):
		return "audio"
	case strings.HasPrefix(mime, "video/"):
		return "video"
	}
	return "file"
}

// texts joins the text items of v, or reports false when v holds anything but
// text.
func (v partsView) onlyText() (string, bool) {
	var b strings.Builder
	for i, it := range v.items {
		if it.kind != "text" {
			return "", false
		}
		if i > 0 {
			b.WriteString("\n")
		}
		b.WriteString(it.text)
	}
	return b.String(), true
}

// messageAttributes flattens one turn into the traceAI
// gen_ai.{input,output}.messages.{i}.message.* keys, the same layout
// traceai-google-genai writes: a text-only turn is message.content, a turn
// with media is message.contents.*, function calls are message.tool_calls.*
// and a function response is message.content plus message.tool_call_id.
func messageAttributes(prefix string, m content, defaultRole string, output bool) []attribute.KeyValue {
	role := m.Role
	if role == "" {
		role = defaultRole
	}
	kvs := []attribute.KeyValue{attribute.String(prefix+MessageRole, role)}
	v := viewParts(m.Parts)

	switch text, onlyText := v.onlyText(); {
	case len(v.items) == 0:
	case onlyText:
		kvs = append(kvs, attribute.String(prefix+MessageContent, text))
	default:
		for k, it := range v.items {
			pp := prefix + MessageContents + "." + strconv.Itoa(k) + "."
			kvs = append(kvs, attribute.String(pp+MessageContentType, it.kind))
			switch {
			case it.kind == "text":
				kvs = append(kvs, attribute.String(pp+MessageContentText, it.text))
			case it.image != "" && !(output && strings.HasPrefix(it.image, "data:")):
				// Generated images (inline bytes in a response) are not
				// copied; their type is.
				kvs = append(kvs, attribute.String(pp+MessageContentImage, it.image))
			}
		}
	}

	switch {
	case len(v.results) == 1 && len(v.items) == 0:
		r := v.results[0]
		kvs = append(kvs, attribute.String(prefix+MessageContent, r.response))
		if r.id != "" {
			kvs = append(kvs, attribute.String(prefix+MessageToolCallID, r.id))
		}
		if r.name != "" {
			kvs = append(kvs, attribute.String(prefix+MessageName, r.name))
		}
	case len(v.results) > 0:
		base := len(v.items)
		for k, r := range v.results {
			pp := prefix + MessageContents + "." + strconv.Itoa(base+k) + "."
			kvs = append(kvs,
				attribute.String(pp+MessageContentType, "text"),
				attribute.String(pp+MessageContentText, r.response))
		}
	}

	for j, call := range v.calls {
		tp := prefix + MessageToolCalls + "." + strconv.Itoa(j) + "."
		if call.id != "" {
			kvs = append(kvs, attribute.String(tp+ToolCallID, call.id))
		}
		if call.name != "" {
			kvs = append(kvs, attribute.String(tp+ToolCallFunctionName, call.name))
		}
		if call.args != "" {
			kvs = append(kvs, attribute.String(tp+ToolCallFunctionArgumentsJSON, call.args))
		}
	}
	return kvs
}

// conversationJSON is the turns as JSON for input.value / output.value: role,
// text, image references (long base64 images redacted), function calls and
// function responses. Audio, video and file bytes are left out.
func conversationJSON(msgs []content, defaultRole string, tc TraceConfig, output bool) string {
	if len(msgs) == 0 {
		return ""
	}
	out := make([]map[string]any, 0, len(msgs))
	for _, m := range msgs {
		out = append(out, simpleMessage(m, defaultRole, tc, output))
	}
	b, err := json.Marshal(out)
	if err != nil {
		return ""
	}
	return string(b)
}

func simpleMessage(m content, defaultRole string, tc TraceConfig, output bool) map[string]any {
	role := m.Role
	if role == "" {
		role = defaultRole
	}
	o := map[string]any{"role": role}
	v := viewParts(m.Parts)
	if text, ok := v.onlyText(); ok && len(v.items) > 0 {
		o["content"] = text
	} else if len(v.items) > 0 {
		items := make([]map[string]any, 0, len(v.items))
		for _, it := range v.items {
			switch {
			case it.kind == "text":
				items = append(items, map[string]any{"type": "text", "text": it.text})
			case it.image != "":
				img := it.image
				if strings.HasPrefix(img, "data:") && (output || len(img) > tc.imageMax()) {
					img = RedactedValue
				}
				items = append(items, map[string]any{"type": "image", "image": img})
			default:
				items = append(items, map[string]any{"type": it.kind})
			}
		}
		o["content"] = items
	}
	if len(v.calls) > 0 {
		calls := make([]map[string]string, 0, len(v.calls))
		for _, c := range v.calls {
			calls = append(calls, map[string]string{"id": c.id, "name": c.name, "arguments": c.args})
		}
		o["tool_calls"] = calls
	}
	if len(v.results) > 0 {
		res := make([]map[string]string, 0, len(v.results))
		for _, r := range v.results {
			res = append(res, map[string]string{"id": r.id, "name": r.name, "response": r.response})
		}
		o["tool_results"] = res
	}
	return o
}

func compactJSON(b json.RawMessage) string {
	if len(b) == 0 || string(b) == "null" {
		return ""
	}
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
