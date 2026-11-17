package wire

import (
	"sort"
	"strings"
)

func openaiUsage(p *parsed, v any)    { p.observeUsage("openai", v) }
func anthropicUsage(p *parsed, v any) { p.observeUsage("anthropic", v) }
func transcript(p *parsed, v any) {
	if s, ok := v.(string); ok {
		p.Extra["gen_ai.audio.transcript"] = s
		p.output(s, "text/plain")
	}
}
func dimension(p *parsed, v any) {
	if a, ok := v.([]any); ok {
		p.Extra["gen_ai.embeddings.dimension.count"] = len(a)
	}
}
func first(v any) any {
	a := arr(v)
	if len(a) > 0 {
		return a[0]
	}
	return nil
}
func setReason(p *parsed, v any) {
	if s := str(v); s != "" {
		p.Reasons = []string{s}
	}
}
func responseOutput(p *parsed, r object) {
	p.ID = str(fallback(r["id"], p.ID))
	p.ResponseModel = str(fallback(r["model"], p.ResponseModel))
	m := message{Role: "assistant"}
	ss := []string{}
	for _, v := range arr(r["output"]) {
		item := obj(v)
		if item["type"] == "message" {
			for _, v := range arr(item["content"]) {
				part := obj(v)
				if part["type"] == "output_text" {
					if s, ok := part["text"].(string); ok {
						ss = append(ss, s)
					}
				}
			}
		} else if item["type"] == "function_call" {
			m.Calls = append(m.Calls, call{str(item["call_id"]), str(item["name"]), args(item["arguments"])})
		}
	}
	m.Content = strings.Join(ss, "\n")
	if len(ss) > 0 || len(m.Calls) > 0 {
		p.Out = []message{m}
	}
	setReason(p, r["status"])
	openaiUsage(p, r["usage"])
}

// eventKind is the event's own type, else the SSE `event:` name (kept by the
// decoder) when the JSON omits its redundant type field.
func eventKind(e object) string { return str(fallback(e["type"], e["event_type"], e["event"])) }

func parseResponse(match *Match, p *parsed, b body) {
	format := match.Endpoint.Format
	r := obj(b.JSON)
	switch format {
	case "openai.chat", "openai.completions":
		parseChat(p, b)
	case "openai.responses":
		if b.Kind == "json" {
			responseOutput(p, r)
		} else if b.Kind == "sse" {
			parseResponsesStream(p, b.Events)
		}
	case "openai.embeddings":
		p.ResponseModel = str(r["model"])
		dimension(p, obj(first(r["data"]))["embedding"])
		openaiUsage(p, r["usage"])
	case "openai.audio_transcription":
		if b.Kind == "text" {
			transcript(p, b.Text)
			return
		}
		if b.Kind == "sse" {
			ss := []string{}
			var done object
			for _, e := range b.Events {
				if eventKind(e) == "transcript.text.delta" {
					ss = append(ss, str(e["delta"]))
				} else if eventKind(e) == "transcript.text.done" {
					done = e
				}
			}
			if done != nil {
				r = done
			} else {
				r = object{"text": strings.Join(ss, "")}
			}
		}
		transcript(p, r["text"])
		put(p.Extra, "gen_ai.audio.language", r["language"])
		if number(r["duration"]) {
			p.Extra["gen_ai.audio.duration_secs"] = r["duration"]
		}
		u := obj(r["usage"])
		if u["type"] == "duration" {
			if _, yes := p.Extra["gen_ai.audio.duration_secs"]; !yes && number(u["seconds"]) {
				p.Extra["gen_ai.audio.duration_secs"] = u["seconds"]
			}
		} else {
			openaiUsage(p, u)
		}
	case "openai.audio_speech", "deepgram.speak", "elevenlabs.tts", "cartesia.tts":
		if s, ok := r["audio_base64"].(string); ok {
			p.Extra["traceai.audio.output_bytes"] = b64Size(s)
		} else if b.Kind == "sse" {
			n := 0
			for _, e := range b.Events {
				if e["type"] == nil || e["type"] == "chunk" {
					n += b64Size(str(e["data"]))
				}
			}
			p.Extra["traceai.audio.output_bytes"] = n
		}
	case "openai.images":
		if b.Kind == "json" {
			// The protocol supplies the media context even for short opaque
			// payloads; recognition/omission still uses the shared S6 predicate.
			p.output(dump(PrivacyValue(r, "", map[string]any{"mime_type": "image/*"}, nil)), "application/json")
			openaiUsage(p, r["usage"])
		}
	case "openai.moderations":
		if b.Kind == "json" {
			p.ID = str(r["id"])
			p.ResponseModel = str(r["model"])
			flagged := false
			for _, v := range arr(r["results"]) {
				flagged = flagged || obj(v)["flagged"] == true
			}
			p.Extra["traceai.moderation.flagged"] = flagged
			p.output(dump(r), "application/json")
		}
	case "rerank":
		if b.Kind == "json" {
			p.ID = str(r["id"])
			p.ResponseModel = str(r["model"])
			ranked := []any{}
			for _, v := range arr(fallback(r["results"], r["data"])) {
				item := obj(v)
				ranked = append(ranked, object{"index": item["index"], "relevance_score": item["relevance_score"]})
			}
			p.output(dump(ranked), "application/json")
			p.usage("total", obj(r["usage"])["total_tokens"])
		}
	case "anthropic.messages":
		parseAnthropic(p, b)
	case "gemini.generate_content":
		items := b.Events
		if b.Kind == "json" {
			items = nil
			if a, ok := b.JSON.([]any); ok {
				for _, v := range a {
					items = append(items, obj(v))
				}
			} else {
				items = []object{r}
			}
		}
		m := message{Role: "assistant"}
		ss := []string{}
		for _, r := range items {
			p.ID = str(fallback(r["responseId"], p.ID))
			p.ResponseModel = str(fallback(r["modelVersion"], p.ResponseModel))
			cs := arr(r["candidates"])
			if len(cs) > 0 {
				out := geminiMessage(obj(obj(cs[0])["content"]))
				if out.Content != "" {
					ss = append(ss, out.Content)
				}
				m.Calls = append(m.Calls, out.Calls...)
				reasons := []string{}
				for _, v := range cs {
					if s := str(obj(v)["finishReason"]); s != "" {
						reasons = append(reasons, s)
					}
				}
				if len(reasons) > 0 {
					p.Reasons = reasons
				}
			}
			p.observeUsage("gemini", r["usageMetadata"])
		}
		m.Content = strings.Join(ss, "")
		if m.Content != "" || len(m.Calls) > 0 {
			p.Out = []message{m}
		}
	case "gemini.embed_content":
		dimension(p, obj(fallback(r["embedding"], first(r["embeddings"])))["values"])
	case "cohere.chat":
		if b.Kind == "sse" || b.Kind == "ndjson" {
			parseCohereStream(p, b.Events)
			return
		}
		if b.Kind != "json" {
			return
		}
		p.ID = str(fallback(r["id"], r["generation_id"]))
		if raw, ok := r["message"].(map[string]any); ok {
			out := openaiMessage(raw)
			if out.Role == "user" && raw["role"] == nil {
				out.Role = "assistant"
			}
			p.Out = []message{out}
		} else if s, ok := r["text"].(string); ok {
			p.Out = []message{{Role: "assistant", Content: s}}
		}
		setReason(p, r["finish_reason"])
		u := obj(fallback(r["usage"], r["meta"]))
		tokens := obj(fallback(u["tokens"], u["billed_units"]))
		p.usage("input", tokens["input_tokens"])
		p.usage("output", tokens["output_tokens"])
	case "cohere.embed":
		em := r["embeddings"]
		if m, ok := em.(map[string]any); ok {
			em = m["float"]
		}
		dimension(p, first(em))
		p.usage("input", obj(obj(r["meta"])["billed_units"])["input_tokens"])
	case "bedrock.converse":
		if raw, ok := obj(r["output"])["message"].(map[string]any); ok {
			p.Out = []message{bedrockMessage(raw)}
		}
		setReason(p, r["stopReason"])
		p.observeUsage("bedrock.converse", r["usage"])
	case "bedrock.invoke":
		if b.Kind == "json" {
			if _, ok := r["content"]; ok {
				parseAnthropic(p, b)
			} else {
				p.output(dump(b.JSON), "application/json")
			}
		}
	case "ollama.chat", "ollama.generate":
		items := b.Events
		if b.Kind == "json" {
			items = []object{r}
		}
		ss := []string{}
		out := message{Role: "assistant"}
		for _, item := range items {
			p.ResponseModel = str(fallback(item["model"], p.ResponseModel))
			m := openaiMessage(obj(item["message"]))
			if m.Content != "" {
				ss = append(ss, m.Content)
			}
			out.Calls = append(out.Calls, m.Calls...)
			if s, ok := item["response"].(string); ok {
				ss = append(ss, s)
			}
			if item["done"] == true {
				setReason(p, item["done_reason"])
				p.usage("input", item["prompt_eval_count"])
				p.usage("output", item["eval_count"])
			}
		}
		out.Content = strings.Join(ss, "")
		if out.Content != "" || len(out.Calls) > 0 {
			p.Out = []message{out}
		}
	case "ollama.embed":
		dimension(p, fallback(first(r["embeddings"]), r["embedding"]))
		p.usage("input", r["prompt_eval_count"])
	case "deepgram.listen":
		meta := obj(r["metadata"])
		p.ID = str(meta["request_id"])
		if number(meta["duration"]) {
			p.Extra["gen_ai.audio.duration_secs"] = meta["duration"]
		}
		ch := obj(first(obj(r["results"])["channels"]))
		transcript(p, obj(first(ch["alternatives"]))["transcript"])
	case "elevenlabs.stt":
		transcript(p, r["text"])
		put(p.Extra, "gen_ai.audio.language", r["language_code"])
		p.ID = str(r["transcription_id"])
	case "assemblyai.transcript":
		if b.Kind != "json" {
			return
		}
		status := str(r["status"])
		if match.Endpoint.OnlyWhen == "completed" && status != "completed" && status != "error" {
			p.Skip = true
			return
		}
		p.ID = str(r["id"])
		p.ResponseModel = str(r["speech_model"])
		setReason(p, status)
		if status == "completed" {
			transcript(p, r["text"])
		}
		if number(r["audio_duration"]) {
			p.Extra["gen_ai.audio.duration_secs"] = r["audio_duration"]
		}
		put(p.Extra, "gen_ai.audio.language", r["language_code"])
	}
}

// Retain function items and argument deltas even when no terminal response
// arrives. Full item/done snapshots replace (never append to) accumulated deltas.
func parseResponsesStream(p *parsed, events []object) {
	var final object
	out := message{Role: "assistant"}
	calls := map[int]*call{}
	itemIndices := map[string]int{}
	for _, e := range events {
		openaiUsage(p, e["usage"])
		openaiUsage(p, obj(e["response"])["usage"])
		n, _ := integer(e["output_index"])
		i := int(n)
		if _, ok := e["output_index"]; !ok {
			i = itemIndices[str(e["item_id"])]
		}
		switch eventKind(e) {
		case "response.completed", "response.incomplete", "response.failed":
			final = obj(e["response"])
		case "response.created", "response.in_progress":
			v := obj(e["response"])
			p.ID = str(fallback(v["id"], p.ID))
			p.ResponseModel = str(fallback(v["model"], p.ResponseModel))
		case "response.output_text.delta":
			out.Content += str(e["delta"])
		case "response.output_item.added", "response.output_item.done":
			item := obj(e["item"])
			if item["type"] != "function_call" {
				continue
			}
			itemIndices[str(item["id"])] = i
			if calls[i] == nil {
				calls[i] = &call{}
			}
			c := calls[i]
			c.ID = str(fallback(item["call_id"], c.ID))
			c.Name = str(fallback(item["name"], c.Name))
			if a, ok := item["arguments"].(string); ok {
				c.Args = a
			}
		case "response.function_call_arguments.delta", "response.function_call_arguments.done":
			if calls[i] == nil {
				calls[i] = &call{}
			}
			if eventKind(e) == "response.function_call_arguments.delta" {
				calls[i].Args += str(e["delta"])
			} else if a, ok := e["arguments"].(string); ok {
				calls[i].Args = a
			}
		}
	}
	indices := make([]int, 0, len(calls))
	for i := range calls {
		indices = append(indices, i)
	}
	sort.Ints(indices)
	for _, i := range indices {
		out.Calls = append(out.Calls, *calls[i])
	}
	if out.Content != "" || len(out.Calls) > 0 {
		p.Out = []message{out}
	}
	if final != nil {
		responseOutput(p, final)
	}
}

func parseCohereStream(p *parsed, events []object) {
	out := message{Role: "assistant"}
	calls := map[int]*call{}
	for _, e := range events {
		delta := obj(e["delta"])
		switch eventKind(e) {
		case "message-start":
			p.ID = str(fallback(e["id"], p.ID))
		case "content-delta":
			out.Content += str(obj(obj(delta["message"])["content"])["text"])
		case "tool-call-start", "tool-call-delta":
			n, _ := integer(e["index"])
			i := int(n)
			if calls[i] == nil {
				calls[i] = &call{}
			}
			raw := obj(obj(delta["message"])["tool_calls"])
			fn := obj(raw["function"])
			c := calls[i]
			c.ID = str(fallback(raw["id"], c.ID))
			c.Name = str(fallback(fn["name"], c.Name))
			c.Args += str(fn["arguments"])
		case "message-end":
			setReason(p, delta["finish_reason"])
			u := obj(obj(delta["usage"])["tokens"])
			p.usage("input", u["input_tokens"])
			p.usage("output", u["output_tokens"])
		case "text-generation":
			out.Content += str(e["text"])
		case "stream-end":
			r := obj(e["response"])
			p.ID = str(fallback(r["generation_id"], r["id"], p.ID))
			setReason(p, fallback(e["finish_reason"], r["finish_reason"]))
			u := obj(obj(r["meta"])["billed_units"])
			p.usage("input", u["input_tokens"])
			p.usage("output", u["output_tokens"])
		}
	}
	indices := make([]int, 0, len(calls))
	for i := range calls {
		indices = append(indices, i)
	}
	sort.Ints(indices)
	for _, i := range indices {
		out.Calls = append(out.Calls, *calls[i])
	}
	if out.Content != "" || len(out.Calls) > 0 {
		p.Out = []message{out}
	}
}

// Count encoded audio without decoding or allocating an audio buffer.
func b64Size(s string) int {
	n, padding := 0, 0
	for _, c := range s {
		if c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '+' || c == '/' {
			n++
		} else if c == '=' {
			padding++
		}
	}
	if (n+padding)%4 != 0 || padding > 2 {
		return 0
	}
	return (n+padding)/4*3 - padding
}
func parseChat(p *parsed, b body) {
	if b.Kind == "json" {
		r := obj(b.JSON)
		p.ID = str(r["id"])
		p.ResponseModel = str(r["model"])
		for _, v := range arr(r["choices"]) {
			c := obj(v)
			if raw, ok := c["message"].(map[string]any); ok {
				p.Out = append(p.Out, openaiMessage(raw))
			} else if s, ok := c["text"].(string); ok {
				p.Out = append(p.Out, message{Role: "assistant", Content: s})
			}
			if s := str(c["finish_reason"]); s != "" {
				p.Reasons = append(p.Reasons, s)
			}
		}
		openaiUsage(p, r["usage"])
		return
	}
	if b.Kind != "sse" {
		return
	}
	slots := map[int]*message{}
	calls := map[int]map[int]*call{}
	finish := map[int]string{}
	for _, chunk := range b.Events {
		p.ID = str(fallback(p.ID, chunk["id"]))
		p.ResponseModel = str(fallback(p.ResponseModel, chunk["model"]))
		openaiUsage(p, chunk["usage"])
		for _, v := range arr(chunk["choices"]) {
			c := obj(v)
			idx, _ := integer(c["index"])
			i := int(idx)
			delta := obj(c["delta"])
			if slots[i] == nil {
				slots[i] = &message{Role: "assistant"}
			}
			m := slots[i]
			if s := str(delta["role"]); s != "" {
				m.Role = s
			}
			m.Content += str(delta["content"]) + str(c["text"])
			for _, v := range arr(delta["tool_calls"]) {
				raw := obj(v)
				n, _ := integer(raw["index"])
				j := int(n)
				if calls[i] == nil {
					calls[i] = map[int]*call{}
				}
				if calls[i][j] == nil {
					calls[i][j] = &call{}
				}
				slot := calls[i][j]
				fn := obj(raw["function"])
				slot.ID = str(fallback(slot.ID, raw["id"]))
				slot.Name = str(fallback(slot.Name, fn["name"]))
				slot.Args += str(fn["arguments"])
			}
			if s := str(c["finish_reason"]); s != "" {
				finish[i] = s
			}
		}
	}
	indices := []int{}
	for i := range slots {
		indices = append(indices, i)
	}
	sort.Ints(indices)
	for _, i := range indices {
		m := slots[i]
		js := []int{}
		for j := range calls[i] {
			js = append(js, j)
		}
		sort.Ints(js)
		for _, j := range js {
			m.Calls = append(m.Calls, *calls[i][j])
		}
		p.Out = append(p.Out, *m)
	}
	indices = nil
	for i := range finish {
		indices = append(indices, i)
	}
	sort.Ints(indices)
	for _, i := range indices {
		p.Reasons = append(p.Reasons, finish[i])
	}
}
func parseAnthropic(p *parsed, b body) {
	if b.Kind == "json" {
		r := obj(b.JSON)
		if _, yes := r["content"]; !yes {
			return
		}
		p.ID = str(r["id"])
		p.ResponseModel = str(r["model"])
		role := str(r["role"])
		if role == "" {
			role = "assistant"
		}
		p.Out = []message{anthropicMessage(role, r["content"])}
		setReason(p, r["stop_reason"])
		anthropicUsage(p, r["usage"])
		return
	}
	if b.Kind != "sse" {
		return
	}
	type block struct {
		raw   object
		parts string
	}
	blocks := map[int]*block{}
	for _, e := range b.Events {
		idx, _ := integer(e["index"])
		i := int(idx)
		switch eventKind(e) {
		case "message_start":
			m := obj(e["message"])
			p.ID = str(m["id"])
			p.ResponseModel = str(m["model"])
			anthropicUsage(p, m["usage"])
		case "content_block_start":
			raw := obj(e["content_block"])
			part := ""
			if raw["type"] == "text" {
				part = str(raw["text"])
			}
			blocks[i] = &block{raw, part}
		case "content_block_delta":
			if blocks[i] == nil {
				blocks[i] = &block{raw: object{"type": "text"}}
			}
			delta := obj(e["delta"])
			if delta["type"] == "text_delta" {
				blocks[i].parts += str(delta["text"])
			} else if delta["type"] == "input_json_delta" {
				blocks[i].parts += str(delta["partial_json"])
			}
		case "message_delta":
			setReason(p, obj(e["delta"])["stop_reason"])
			anthropicUsage(p, e["usage"])
		}
	}
	indices := []int{}
	for i := range blocks {
		indices = append(indices, i)
	}
	sort.Ints(indices)
	out := message{Role: "assistant"}
	texts := []string{}
	for _, i := range indices {
		b := blocks[i]
		switch str(b.raw["type"]) {
		case "text":
			texts = append(texts, b.parts)
		case "tool_use":
			arguments := b.parts
			if arguments == "" {
				arguments = args(b.raw["input"])
			}
			out.Calls = append(out.Calls, call{str(b.raw["id"]), str(b.raw["name"]), arguments})
		}
	}
	out.Content = strings.Join(texts, "\n")
	if out.Content != "" || len(out.Calls) > 0 {
		p.Out = []message{out}
	}
}
