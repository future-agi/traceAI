package wire

import (
	"bytes"
	"io"
	"mime"
	"mime/multipart"
	"strings"
)

// ReadMultipart drains file parts without retaining bytes. Text fields have an
// independent 64 KiB cap, so a large prompt/file cannot hide a later model field.
// Names/descriptors consume a separate bounded metadata budget. At most 64 text
// fields and 128 file descriptors are retained, even for adversarial empty parts.
func ReadMultipart(r io.Reader, contentType string, limit int64) (map[string]string, []FilePart, bool) {
	fields := map[string]string{}
	files := []FilePart{}
	_, params, err := mime.ParseMediaType(contentType)
	if err != nil {
		return fields, files, true
	}
	mr := multipart.NewReader(r, params["boundary"])
	left := limit
	if left > DefaultMaxBodyBytes {
		left = DefaultMaxBodyBytes
	}
	truncated := false
	for {
		part, err := mr.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			truncated = true
			break
		}
		name := part.FormName()
		if part.FileName() != "" || Binary(strings.ToLower(strings.TrimSpace(strings.Split(part.Header.Get("Content-Type"), ";")[0]))) {
			size, copyErr := io.Copy(io.Discard, part)
			f := FilePart{name, part.FileName(), part.Header.Get("Content-Type"), size}
			cost := int64(len(f.Name) + len(f.Filename) + len(f.ContentType) + 64)
			if cost <= left && len(files) < 128 {
				files = append(files, f)
				left -= cost
			} else {
				truncated = true
			}
			truncated = truncated || copyErr != nil
		} else {
			const fieldLimit = 64 * 1024
			b, readErr := io.ReadAll(io.LimitReader(part, fieldLimit+1))
			if len(b) > fieldLimit {
				b = b[:fieldLimit]
				truncated = true
			}
			// Text is independent of the file-metadata/body budget. Names and
			// cardinality are capped separately; duplicate names replace values.
			if len(name) <= 1024 && (len(fields) < 64 || fields[name] != "") {
				fields[name] = string(b)
			} else {
				truncated = true
			}
			_, copyErr := io.Copy(io.Discard, part)
			truncated = truncated || readErr != nil || copyErr != nil
		}
		_ = part.Close()
	}
	return fields, files, truncated
}
func openaiMessage(raw object) message {
	role := str(raw["role"])
	if role == "" {
		role = "user"
	}
	m := message{Role: role, Content: text(raw["content"]), ToolID: str(raw["tool_call_id"])}
	for _, v := range arr(raw["tool_calls"]) {
		c := obj(v)
		f := obj(c["function"])
		m.Calls = append(m.Calls, call{str(c["id"]), str(f["name"]), args(f["arguments"])})
	}
	return m
}
func anthropicMessage(role string, content any) message {
	m := message{Role: role}
	if s, ok := content.(string); ok {
		m.Content = s
		return m
	}
	ss := []string{}
	for _, v := range arr(content) {
		b := obj(v)
		switch str(b["type"]) {
		case "text":
			if s, ok := b["text"].(string); ok {
				ss = append(ss, s)
			}
		case "tool_use":
			m.Calls = append(m.Calls, call{str(b["id"]), str(b["name"]), args(b["input"])})
		case "tool_result":
			m.ToolID = str(b["tool_use_id"])
			if s := text(b["content"]); s != "" {
				ss = append(ss, s)
			}
		}
	}
	m.Content = strings.Join(ss, "\n")
	return m
}
func geminiMessage(raw object) message {
	role := str(raw["role"])
	if role == "" {
		role = "user"
	}
	if role == "model" {
		role = "assistant"
	}
	m := message{Role: role}
	ss := []string{}
	for _, v := range arr(raw["parts"]) {
		b := obj(v)
		if s, ok := b["text"].(string); ok && b["thought"] != true {
			ss = append(ss, s)
		} else if c, ok := b["functionCall"].(map[string]any); ok {
			m.Calls = append(m.Calls, call{str(c["id"]), str(c["name"]), args(c["args"])})
		} else if c, ok := b["functionResponse"].(map[string]any); ok {
			m.Role = "tool"
			m.ToolID = str(fallback(c["id"], c["name"]))
			ss = append(ss, args(c["response"]))
		}
	}
	m.Content = strings.Join(ss, "\n")
	return m
}
func bedrockMessage(raw object) message {
	role := str(raw["role"])
	if role == "" {
		role = "user"
	}
	m := message{Role: role}
	ss := []string{}
	for _, v := range arr(raw["content"]) {
		b := obj(v)
		if s, ok := b["text"].(string); ok {
			ss = append(ss, s)
		} else if c, ok := b["toolUse"].(map[string]any); ok {
			m.Calls = append(m.Calls, call{str(c["toolUseId"]), str(c["name"]), args(c["input"])})
		} else if c, ok := b["toolResult"].(map[string]any); ok {
			m.ToolID = str(c["toolUseId"])
			for _, v := range arr(c["content"]) {
				if s := text(v); s != "" {
					ss = append(ss, s)
				}
			}
		}
	}
	m.Content = strings.Join(ss, "\n")
	return m
}
func commonParams(b object, p *parsed) {
	for _, k := range strings.Fields("temperature top_p frequency_penalty presence_penalty seed") {
		if number(b[k]) {
			p.Params[k] = b[k]
		}
	}
	for _, k := range strings.Fields("max_tokens max_completion_tokens max_output_tokens") {
		if number(b[k]) {
			p.Params["max_tokens"] = b[k]
			break
		}
	}
}
func mapParams(b object, p *parsed, mapping map[string]string) {
	for src, dst := range mapping {
		if number(b[src]) {
			p.Params[dst] = b[src]
		}
	}
}
func check(m *Match, req Request) *parsed {
	p := newParsed()
	format := m.Endpoint.Format
	multipartKey := ""
	switch format {
	case "openai.audio_transcription":
		multipartKey = "model"
	case "elevenlabs.stt":
		multipartKey = "model_id"
	}
	noBodyRequired := format == "deepgram.listen" || format == "assemblyai.transcript"
	known := resolve(m.Host, m.Port) != ""
	if req.Unavailable || req.Truncated && req.Fields == nil && !strings.Contains(ContentType(req.Header), "multipart/") {
		if !known {
			return nil
		}
		// No content from an unavailable/oversized body may escape through
		// parameters, tools, or flattened messages.
	} else if multipartKey != "" || req.Fields != nil || strings.Contains(ContentType(req.Header), "multipart/") {
		fields := req.Fields
		if fields == nil && req.Body != nil && strings.Contains(ContentType(req.Header), "multipart/") {
			fields, req.Files, _ = ReadMultipart(bytes.NewReader(req.Body), req.Header.Get("Content-Type"), DefaultMaxBodyBytes)
		}
		credentialBudget := newPrivacyBudget()
		for key, value := range fields {
			observeCredentials(value, key, req.ObserveSecret, credentialBudget, 0)
		}
		if multipartKey != "" && fields[multipartKey] == "" {
			if !req.Truncated || !known {
				return nil
			}
			p.Extra["traceai.wire.truncated"] = true
			return p
		}
		if multipartKey == "" {
			body := object{}
			for k, v := range fields {
				body[k] = v
				if decoded := decode([]byte(v)); decoded != nil {
					body[k] = decoded
				}
			}
			observeCredentials(body, "", req.ObserveSecret, credentialBudget, 0)
			if !parseRequest(format, body, p) {
				return nil
			}
		}
		modelKey := multipartKey
		if modelKey == "" {
			modelKey = "model"
		}
		p.RequestModel = fields[modelKey]
		put(p.Extra, "gen_ai.audio.language", fallback(fields["language"], fields["language_code"]))
		params, described := object{}, object{}
		for k, v := range fields {
			described[k] = v
			if !contains(contentKeys, k) && k != multipartKey {
				params[k] = v
			}
		}
		if len(params) > 0 {
			p.Params["_json"] = params
		}
		for _, f := range req.Files {
			described[f.Name] = object{"filename": f.Filename, "content_type": f.ContentType, "bytes": f.Size}
		}
		p.input(dump(described), "application/json")
	} else if Binary(ContentType(req.Header)) {
		if format != "deepgram.listen" && format != "assemblyai.transcript" {
			return nil
		}
		put(p.Extra, "gen_ai.audio.mime_type", ContentType(req.Header))
		p.Extra["traceai.audio.input_bytes"] = req.Size
	} else if req.Body != nil && (len(req.Body) > 0 || !noBodyRequired) {
		decoded := decode(req.Body)
		observeCredentials(decoded, "", req.ObserveSecret, newPrivacyBudget(), 0)
		b, valid := decoded.(map[string]any)
		if !valid {
			return nil
		} else {
			if !parseRequest(format, b, p) {
				return nil
			}
			if !p.HasInput {
				p.input(string(req.Body), "application/json")
			}
			for _, k := range strings.Fields("model model_id speech_model modelId") {
				if s, ok := b[k].(string); ok {
					p.RequestModel = s
					break
				}
			}
			if params := parameters(b); len(params) > 0 {
				p.Params["_json"] = params
			}
		}
	} else if !noBodyRequired {
		if !known {
			return nil
		}
		p.Extra["traceai.wire.body_unavailable"] = true
	}
	if p.RequestModel == "" {
		p.RequestModel = m.Groups["model"]
		if p.RequestModel == "" {
			p.RequestModel = m.Query.Get("model")
		}
	}
	if format == "deepgram.listen" {
		put(p.Extra, "gen_ai.audio.language", m.Query.Get("language"))
	}
	put(p.Extra, "gen_ai.request.voice", m.Groups["voice"])
	if len(req.Files) > 0 {
		put(p.Extra, "gen_ai.audio.mime_type", req.Files[0].ContentType)
		p.Extra["traceai.audio.input_bytes"] = req.Files[0].Size
	}
	return p
}
func parseRequest(format string, b object, p *parsed) bool {
	p.Tools = arr(b["tools"])
	switch format {
	case "openai.chat", "ollama.chat":
		if ms, ok := b["messages"].([]any); ok {
			for _, v := range ms {
				if m, ok := v.(map[string]any); ok {
					p.In = append(p.In, openaiMessage(m))
				}
			}
		} else if s, ok := b["prompt"].(string); ok && format == "openai.chat" {
			p.In = []message{{Role: "user", Content: s}}
		} else {
			return false
		}
		commonParams(b, p)
	case "openai.completions":
		if b["prompt"] == nil {
			return false
		}
		p.In = []message{{Role: "user", Content: text(b["prompt"])}}
		commonParams(b, p)
	case "openai.responses":
		_, hasInput := b["input"]
		_, hasModel := b["model"]
		if !hasInput && !hasModel {
			return false
		}
		if s, ok := b["instructions"].(string); ok {
			p.In = append(p.In, message{Role: "system", Content: s})
		}
		if s, ok := b["input"].(string); ok {
			p.In = append(p.In, message{Role: "user", Content: s})
		} else {
			for _, v := range arr(b["input"]) {
				raw := obj(v)
				switch str(raw["type"]) {
				case "function_call_output":
					p.In = append(p.In, message{Role: "tool", Content: args(raw["output"]), ToolID: str(raw["call_id"])})
				case "function_call":
					p.In = append(p.In, message{Role: "assistant", Calls: []call{{str(raw["call_id"]), str(raw["name"]), args(raw["arguments"])}}})
				default:
					if r, ok := raw["role"].(string); ok {
						p.In = append(p.In, message{Role: r, Content: text(raw["content"])})
					}
				}
			}
		}
		commonParams(b, p)
	case "openai.embeddings", "ollama.embed":
		v := b["input"]
		if format == "ollama.embed" && v == nil {
			v = b["prompt"]
		}
		if v == nil {
			return false
		}
		n := 1
		if a, ok := v.([]any); ok {
			if format == "ollama.embed" || (len(a) > 0 && !number(a[0])) {
				n = len(a)
			}
		}
		p.Extra["gen_ai.request.embedding_inputs_count"] = n
	case "openai.audio_speech", "deepgram.speak", "elevenlabs.tts", "cartesia.tts":
		key := "text"
		if format == "openai.audio_speech" {
			key = "input"
		}
		if format == "cartesia.tts" {
			key = "transcript"
		}
		s, ok := b[key].(string)
		if !ok {
			return false
		}
		p.input(s, "text/plain")
		p.Extra["gen_ai.audio.transcript"] = s
		if format == "openai.audio_speech" {
			put(p.Extra, "gen_ai.request.voice", b["voice"])
		}
	case "openai.images", "ollama.generate":
		s, ok := b["prompt"].(string)
		if !ok {
			return false
		}
		if format == "ollama.generate" {
			if s, ok := b["system"].(string); ok {
				p.In = append(p.In, message{Role: "system", Content: s})
			}
		}
		p.In = append(p.In, message{Role: "user", Content: s})
	case "openai.moderations":
		return b["input"] != nil
	case "rerank":
		query, ok := b["query"].(string)
		docs, ok2 := b["documents"].([]any)
		if !ok || !ok2 {
			return false
		}
		p.In = []message{{Role: "user", Content: query}}
		put(p.Extra, "gen_ai.request.top_n", fallback(b["top_n"], b["top_k"]))
		p.Extra["gen_ai.rerank.documents_count"] = len(docs)
	case "anthropic.messages", "bedrock.invoke":
		ms, ok := b["messages"].([]any)
		if !ok {
			return format == "bedrock.invoke"
		}
		if s := text(b["system"]); s != "" {
			p.In = append(p.In, message{Role: "system", Content: s})
		}
		for _, v := range ms {
			raw := obj(v)
			role := str(raw["role"])
			if role == "" {
				role = "user"
			}
			p.In = append(p.In, anthropicMessage(role, raw["content"]))
		}
		commonParams(b, p)
	case "gemini.generate_content":
		cs := b["contents"]
		if cs == nil {
			return false
		}
		if s := text(fallback(b["systemInstruction"], b["system_instruction"])); s != "" {
			p.In = append(p.In, message{Role: "system", Content: s})
		}
		if s, ok := cs.(string); ok {
			p.In = append(p.In, message{Role: "user", Content: s})
		} else {
			items, ok := cs.([]any)
			if !ok {
				items = []any{cs}
			}
			for _, v := range items {
				if m, ok := v.(map[string]any); ok {
					p.In = append(p.In, geminiMessage(m))
				}
			}
		}
		mapParams(obj(fallback(b["generationConfig"], b["generation_config"])), p, map[string]string{"temperature": "temperature", "topP": "top_p", "maxOutputTokens": "max_tokens", "seed": "seed", "frequencyPenalty": "frequency_penalty", "presencePenalty": "presence_penalty"})
	case "gemini.embed_content":
		if _, ok := b["content"]; ok {
			p.Extra["gen_ai.request.embedding_inputs_count"] = 1
		} else if a, ok := b["requests"].([]any); ok {
			p.Extra["gen_ai.request.embedding_inputs_count"] = len(a)
		} else {
			return false
		}
	case "cohere.chat":
		if ms, ok := b["messages"].([]any); ok {
			for _, v := range ms {
				p.In = append(p.In, openaiMessage(obj(v)))
			}
		} else if s, ok := b["message"].(string); ok {
			if s, ok := b["preamble"].(string); ok {
				p.In = append(p.In, message{Role: "system", Content: s})
			}
			for _, v := range arr(b["chat_history"]) {
				raw := obj(v)
				role := map[string]string{"USER": "user", "CHATBOT": "assistant", "SYSTEM": "system"}[strings.ToUpper(str(raw["role"]))]
				if role == "" {
					role = "user"
				}
				p.In = append(p.In, message{Role: role, Content: text(raw["message"])})
			}
			p.In = append(p.In, message{Role: "user", Content: s})
		} else {
			return false
		}
		commonParams(b, p)
		if number(b["p"]) {
			p.Params["top_p"] = b["p"]
		}
	case "cohere.embed":
		items, ok := fallback(b["texts"], b["inputs"], b["images"]).([]any)
		if !ok {
			return false
		}
		p.Extra["gen_ai.request.embedding_inputs_count"] = len(items)
	case "bedrock.converse":
		ms, ok := b["messages"].([]any)
		if !ok {
			return false
		}
		if s := text(b["system"]); s != "" {
			p.In = append(p.In, message{Role: "system", Content: s})
		}
		for _, v := range ms {
			p.In = append(p.In, bedrockMessage(obj(v)))
		}
		mapParams(obj(b["inferenceConfig"]), p, map[string]string{"temperature": "temperature", "topP": "top_p", "maxTokens": "max_tokens"})
		p.Tools = arr(obj(b["toolConfig"])["tools"])
	case "deepgram.listen", "assemblyai.transcript":
	default:
		return false
	}
	if strings.HasPrefix(format, "ollama.") {
		mapParams(obj(b["options"]), p, map[string]string{"temperature": "temperature", "top_p": "top_p", "num_predict": "max_tokens", "seed": "seed"})
	}
	return true
}
