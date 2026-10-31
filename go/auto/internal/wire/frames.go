package wire

import (
	"bytes"
	"encoding/json"
	"math"
	"sort"
	"strings"
)

// Arrival is one observed, non-empty transport chunk: the cumulative captured
// byte offset after it and its arrival time in seconds since request handoff.
// The transport records it before any copying, decoding or parsing (A10).
type Arrival struct {
	End     int64
	Seconds float64
}

// MaxArrivals bounds the per-response arrival association. Past it the
// transport stops recording and sets Response.ArrivalsLost.
const MaxArrivals = 4096

// frame is one complete wire frame: an SSE event (blank-line terminated), an
// NDJSON record (newline terminated) or a JSON value/array member (closing
// brace). end is the byte offset just past the frame's completing byte.
type frame struct {
	event object // nil when the frame is not a JSON object ([DONE], arrays, ...)
	end   int
}

// sseFrames scans raw bytes, so frame ends stay valid transport offsets. CRLF,
// LF and CR terminate lines; a blank line completes an event. A trailing frame
// without its blank line is still partial and is never returned.
func sseFrames(data []byte) []frame {
	var frames []frame
	var lines [][]byte
	pos := 0
	for pos < len(data) {
		i := bytes.IndexAny(data[pos:], "\r\n")
		if i < 0 {
			break
		}
		line := data[pos : pos+i]
		next := pos + i + 1
		if data[pos+i] == '\r' && next < len(data) && data[next] == '\n' {
			next++
		}
		pos = next
		if len(line) > 0 {
			lines = append(lines, line)
			continue
		}
		if len(lines) == 0 {
			continue
		}
		f := frame{end: pos}
		var payload [][]byte
		name, named := "", false
		for _, l := range lines {
			if rest, ok := bytes.CutPrefix(l, []byte("data:")); ok {
				payload = append(payload, bytes.TrimPrefix(rest, []byte(" ")))
			} else if rest, ok := bytes.CutPrefix(l, []byte("event:")); ok {
				name, named = strings.TrimSpace(string(rest)), true
			}
		}
		lines = nil
		if v, ok := decode(bytes.Join(payload, []byte("\n"))).(map[string]any); ok {
			// Honor the SSE event name when JSON omits its redundant type.
			if named {
				v["event"] = name
			}
			f.event = v
		}
		frames = append(frames, f)
	}
	return frames
}

// ndjsonFrames returns only newline-terminated records. Even a complete JSON
// object without its newline is a partial line (failure or EOF before it).
func ndjsonFrames(data []byte) []frame {
	var frames []frame
	pos := 0
	for {
		i := bytes.IndexByte(data[pos:], '\n')
		if i < 0 {
			return frames
		}
		line := data[pos : pos+i]
		pos += i + 1
		if len(bytes.TrimSpace(line)) == 0 {
			continue
		}
		v := decode(line)
		if v == nil {
			continue
		}
		m, _ := v.(map[string]any)
		frames = append(frames, frame{event: m, end: pos})
	}
}

// jsonFrames decodes one JSON value, or each complete member of a top-level
// array (its closing brace completes it, independently of the later comma or
// bracket), in ONE pass. It stops at the first incomplete/invalid member and
// never salvages fields from a partial one. value is the decoded document (or
// the complete-member prefix of an array); complete reports that the whole body
// was exactly that value plus whitespace.
func jsonFrames(data []byte) (value any, frames []frame, array, complete bool) {
	trimmed := bytes.TrimLeft(data, " \t\r\n")
	d := json.NewDecoder(bytes.NewReader(data))
	rest := func() bool { return len(bytes.TrimLeft(data[d.InputOffset():], " \t\r\n")) == 0 }
	if len(trimmed) > 0 && trimmed[0] == '[' {
		if token, err := d.Token(); err != nil || token != json.Delim('[') {
			return nil, nil, true, false
		}
		items := []any{}
		for d.More() {
			var item any
			if d.Decode(&item) != nil {
				return items, frames, true, false
			}
			m, _ := item.(map[string]any)
			items = append(items, item)
			frames = append(frames, frame{event: m, end: int(d.InputOffset())})
		}
		token, err := d.Token()
		return items, frames, true, err == nil && token == json.Delim(']') && rest()
	}
	if d.Decode(&value) != nil {
		return nil, nil, false, false
	}
	m, _ := value.(map[string]any)
	return value, []frame{{event: m, end: int(d.InputOffset())}}, false, rest()
}

func eventsOf(frames []frame) []object {
	var events []object
	for _, f := range frames {
		if f.event != nil {
			events = append(events, f.event)
		}
	}
	return events
}

// arrivalAt returns the arrival time of the chunk that completed byte offset end.
func arrivalAt(arrivals []Arrival, end int) (float64, bool) {
	i := sort.Search(len(arrivals), func(i int) bool { return arrivals[i].End >= int64(end) })
	if i == len(arrivals) {
		return 0, false
	}
	return arrivals[i].Seconds, true
}

func roundSeconds(s float64) float64 { return math.Round(s*1e6) / 1e6 }

// streamMilestones computes both A10 milestones from transport observations:
// first non-empty chunk, and the arrival of the chunk completing the first
// frame that carries non-empty model output. Parse time is never substituted.
// encoded bodies have arrival offsets in compressed space: first-text is
// omitted. A lost association (bounded record overflow) omits first-text and
// marks the span incomplete.
func streamMilestones(resp Response, b body, encoded bool) object {
	a := object{}
	if resp.FirstChunkSeconds != nil {
		a["gen_ai.response.time_to_first_chunk"] = roundSeconds(*resp.FirstChunkSeconds)
	}
	if encoded || len(resp.Arrivals) == 0 {
		return a
	}
	for _, f := range b.frames {
		if !firstOutput(f.event) {
			continue
		}
		if seconds, ok := arrivalAt(resp.Arrivals, f.end); ok {
			a["gen_ai.response.time_to_first_text"] = roundSeconds(seconds)
		} else if resp.ArrivalsLost {
			a["traceai.wire.incomplete"] = true
		}
		break
	}
	return a
}

// --- A10 recognizer: a 1:1 port of the spec oracle's first_output. ---

func nonempty(v any) bool { s, ok := v.(string); return ok && s != "" }

// truthy follows the oracle's Python `or` chains: None, false, 0, "", [] and
// {} are falsy, everything else is truthy.
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case float64:
		return x != 0
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}
func or(vs ...any) any {
	for _, v := range vs {
		if truthy(v) {
			return v
		}
	}
	return vs[len(vs)-1]
}
func dict(v any) object { m, _ := or(v, nil).(map[string]any); return m }
func list(v any) []any  { a, _ := or(v, nil).([]any); return a }

func toolOutput(function any, arguments string) bool {
	f, ok := function.(map[string]any)
	if !ok {
		return false
	}
	value := f[arguments]
	if nonempty(f["name"]) || nonempty(value) {
		return true
	}
	m, ok := value.(map[string]any)
	return ok && len(m) > 0
}
func toolCallsOutput(calls any) bool {
	if m, ok := calls.(map[string]any); ok {
		calls = []any{m}
	}
	for _, c := range list(calls) {
		if m, ok := c.(map[string]any); ok && toolOutput(m["function"], "arguments") {
			return true
		}
	}
	return false
}
func contentOutput(content any) bool {
	if nonempty(content) {
		return true
	}
	var parts []any
	switch x := content.(type) {
	case map[string]any:
		parts = []any{x}
	case []any:
		parts = x
	}
	for _, raw := range parts {
		part, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if kind := part["type"]; kind != nil && kind != "text" && kind != "thinking" {
			continue
		}
		if nonempty(part["text"]) || nonempty(part["thinking"]) {
			return true
		}
		if thinking, ok := part["thinking"].([]any); ok {
			for _, piece := range thinking {
				if p, ok := piece.(map[string]any); ok && p["type"] == "text" && nonempty(p["text"]) {
					return true
				}
			}
		}
	}
	return false
}

var responsesDeltas = strings.Fields("response.output_text.delta response.refusal.delta response.reasoning_text.delta response.reasoning_summary_text.delta response.function_call_arguments.delta response.mcp_call_arguments.delta response.code_interpreter_call_code.delta response.custom_tool_call_input.delta response.audio.delta response.audio.transcript.delta")

// firstOutput reports whether one decoded event carries non-empty model output:
// text, refusal, reasoning, tool name/arguments, code, or generated audio. IDs,
// roles, empty scaffolds, usage, finish events and tool results do not.
func firstOutput(event object) bool {
	if event == nil {
		return false
	}
	for _, raw := range list(event["choices"]) {
		choice := obj(raw)
		if nonempty(choice["text"]) {
			return true
		}
		delta := dict(choice["delta"])
		if contentOutput(delta["content"]) || nonempty(delta["refusal"]) || nonempty(delta["reasoning_content"]) || nonempty(delta["reasoning"]) {
			return true
		}
		audio := dict(delta["audio"])
		if nonempty(audio["data"]) || nonempty(audio["transcript"]) {
			return true
		}
		if toolCallsOutput(delta["tool_calls"]) || toolOutput(delta["function_call"], "arguments") {
			return true
		}
	}
	kind := or(event["type"], event["event_type"], event["event"])
	if kind == "response.output_item.added" {
		item := dict(event["item"])
		switch item["type"] {
		case "function_call", "tool_call", "custom_tool_call", "mcp_call":
			if toolOutput(item, "arguments") || toolOutput(item["function"], "arguments") {
				return true
			}
		}
		if item["type"] == "custom_tool_call" && nonempty(item["input"]) || item["type"] == "code_interpreter_call" && nonempty(item["code"]) {
			return true
		}
	}
	if s, ok := kind.(string); ok && contains(responsesDeltas, s) && nonempty(event["delta"]) {
		return true
	}
	if kind == "content_block_start" {
		block := dict(event["content_block"])
		if nonempty(block["text"]) || nonempty(block["thinking"]) {
			return true
		}
		switch block["type"] {
		case "tool_use", "server_tool_use", "mcp_tool_use":
			if nonempty(block["name"]) {
				return true
			}
		}
	}
	if kind == "content_block_delta" {
		delta := dict(event["delta"])
		if nonempty(delta["text"]) || nonempty(delta["thinking"]) || nonempty(delta["partial_json"]) {
			return true
		}
	}
	for _, raw := range list(event["candidates"]) {
		for _, p := range list(dict(obj(raw)["content"])["parts"]) {
			part := obj(p)
			if nonempty(part["text"]) || toolOutput(part["functionCall"], "args") {
				return true
			}
			if code, ok := or(part["executableCode"], nil).(map[string]any); ok && nonempty(code["code"]) {
				return true
			}
			inline := dict(or(part["inlineData"], part["inline_data"]))
			if mime, ok := or(inline["mimeType"], inline["mime_type"]).(string); ok && strings.HasPrefix(strings.ToLower(mime), "audio/") && nonempty(inline["data"]) {
				return true
			}
		}
	}
	delta := dict(dict(event["contentBlockDelta"])["delta"])
	if nonempty(delta["text"]) || toolOutput(delta["toolUse"], "input") || nonempty(dict(delta["reasoningContent"])["text"]) {
		return true
	}
	if toolOutput(dict(dict(event["contentBlockStart"])["start"])["toolUse"], "input") {
		return true
	}
	switch kind {
	case "content-start", "content-delta":
		if contentOutput(dict(dict(event["delta"])["message"])["content"]) {
			return true
		}
	case "tool-plan-delta":
		if nonempty(dict(dict(event["delta"])["message"])["tool_plan"]) {
			return true
		}
	case "tool-call-start", "tool-call-delta":
		if toolCallsOutput(dict(dict(event["delta"])["message"])["tool_calls"]) {
			return true
		}
	case "tool-calls-generation":
		for _, call := range list(event["tool_calls"]) {
			if toolOutput(call, "parameters") {
				return true
			}
		}
	case "tool-calls-chunk":
		d := event["tool_call_delta"]
		if nonempty(event["text"]) || toolOutput(d, "parameters") || nonempty(obj(d)["text"]) {
			return true
		}
	case "text-generation":
		if nonempty(event["text"]) {
			return true
		}
	case nil:
		// Ollama records are untyped. A typed keep-alive/error event's message
		// or response string is not model output.
		if message, ok := event["message"].(map[string]any); ok && (nonempty(message["content"]) || nonempty(message["thinking"]) || toolCallsOutput(message["tool_calls"])) {
			return true
		}
		if nonempty(event["response"]) || nonempty(event["thinking"]) {
			return true
		}
	}
	// Cartesia TTS SSE emits generated base64 audio in chunk.data.
	return kind == "chunk" && nonempty(event["data"])
}
