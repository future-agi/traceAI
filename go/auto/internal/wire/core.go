// Package wire implements the shared traceAI HTTP exchange contract. It has no
// OpenTelemetry dependency; transports collect bounded bodies and timings only.
package wire

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

//go:embed wire-protocols.json
var CatalogJSON []byte

const DefaultMaxBodyBytes = 4 * 1024 * 1024

type provider struct {
	Name     string   `json:"name"`
	Hosts    []string `json:"hosts"`
	Suffixes []string `json:"host_suffixes"`
	Prefixes []string `json:"host_prefixes"`
	Ports    []int    `json:"ports"`
	Match    string   `json:"match"`
}
type Endpoint struct {
	ID        string   `json:"id"`
	Methods   []string `json:"methods"`
	Path      string   `json:"path"`
	Format    string   `json:"format"`
	Operation string   `json:"operation"`
	SpanKind  string   `json:"span_kind"`
	Providers []string `json:"providers"`
	Exclude   []string `json:"exclude_providers"`
	OnlyWhen  string   `json:"only_when"`
	// DefaultProvider names the provider for a host no catalog provider owns (a proxy or gateway).
	DefaultProvider string `json:"default_provider"`
	re              *regexp.Regexp
}
type catalog struct {
	Default   string     `json:"default_provider"`
	Providers []provider `json:"providers"`
	Endpoints []Endpoint `json:"endpoints"`
}

var protocols = loadCatalog()

func loadCatalog() catalog {
	var c catalog
	if json.Unmarshal(CatalogJSON, &c) != nil {
		return c
	}
	for i := range c.Endpoints {
		c.Endpoints[i].re, _ = regexp.Compile(c.Endpoints[i].Path)
	}
	return c
}
func contains(ss []string, s string) bool {
	for _, v := range ss {
		if v == s {
			return true
		}
	}
	return false
}
func resolve(host string, port int) string {
	name, best := "", 4
	for _, p := range protocols.Providers {
		checks := []bool{}
		priority := 4
		add := func(match bool, rank int) {
			checks = append(checks, match)
			if match && rank < priority {
				priority = rank
			}
		}
		if len(p.Hosts) > 0 {
			add(contains(p.Hosts, host), 0)
		}
		if len(p.Suffixes) > 0 {
			yes := false
			for _, s := range p.Suffixes {
				yes = yes || strings.HasSuffix(host, s)
			}
			add(yes, 1)
		}
		if len(p.Prefixes) > 0 {
			yes := false
			for _, s := range p.Prefixes {
				yes = yes || strings.HasPrefix(host, s)
			}
			add(yes, 2)
		}
		if len(p.Ports) > 0 {
			yes := false
			for _, v := range p.Ports {
				yes = yes || v == port
			}
			add(yes, 3)
		}
		yes := p.Match == "all" && len(checks) > 0
		for _, v := range checks {
			if p.Match == "all" {
				yes = yes && v
			} else {
				yes = yes || v
			}
		}
		if yes && priority < best {
			name, best = p.Name, priority
		}
	}
	return name
}

type Match struct {
	Endpoint       Endpoint
	Provider, Host string
	Port           int
	Groups         map[string]string
	Query          url.Values
}

func Find(method, rawURL string) *Match {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil
	}
	host := strings.ToLower(u.Hostname())
	port := 0
	if u.Port() != "" {
		port, err = strconv.Atoi(u.Port())
		if err != nil {
			return nil
		}
	} else if u.Scheme == "https" {
		port = 443
	} else if u.Scheme == "http" {
		port = 80
	}
	hostProvider := resolve(host, port)
	// EscapedPath preserves encoded model slashes until the named group is extracted.
	path := u.EscapedPath()
	if path == "" {
		path = "/"
	}
	for _, e := range protocols.Endpoints {
		p := hostProvider
		if p == "" {
			p = e.DefaultProvider
		}
		if p == "" {
			p = protocols.Default
		}
		if e.re == nil || !contains(e.Methods, strings.ToUpper(method)) || (len(e.Providers) > 0 && !contains(e.Providers, p)) || contains(e.Exclude, p) {
			continue
		}
		found := e.re.FindStringSubmatch(path)
		if found == nil {
			continue
		}
		groups := map[string]string{}
		for i, n := range e.re.SubexpNames() {
			if i > 0 && n != "" {
				groups[n], _ = url.PathUnescape(found[i])
			}
		}
		return &Match{e, p, host, port, groups, u.Query()}
	}
	return nil
}

type FilePart struct {
	Name, Filename, ContentType string
	Size                        int64
}
type Request struct {
	Method, URL string
	Header      http.Header
	Body        []byte
	Fields      map[string]string
	Files       []FilePart
	Size        int64
	Truncated   bool
	Unavailable bool
	Secrets     []string
	// ObserveSecret receives credential fields from bodies the agent already
	// parsed. Nil keeps the standalone wire core's per-exchange privacy behavior.
	ObserveSecret     func(string)
	MaxBodyBytes      int64
	UndecodedEncoding string
}
type Response struct {
	Status            int
	Header            http.Header
	Body              []byte
	Size              int64
	Truncated         bool
	FirstChunkSeconds *float64
	// Arrivals associates captured byte offsets with transport arrival times
	// (A10). ArrivalsLost reports that the bounded record overflowed.
	Arrivals     []Arrival
	ArrivalsLost bool
	Error        error
	Incomplete   bool
}
type Result struct {
	Name        string
	Attributes  map[string]any
	OK          bool
	Description string
}

func ContentType(h http.Header) string {
	return strings.ToLower(strings.TrimSpace(strings.Split(h.Get("Content-Type"), ";")[0]))
}
func Binary(ct string) bool {
	// Binary admission uses the catalog's MIME predicate too. Amazon eventstream
	// is a framing protocol we cannot decode, not a separate media-name list.
	return hint(privacyRules.Media.MIME.Names[0], ct).media || ct == "application/vnd.amazon.eventstream"
}

type object = map[string]any

func obj(v any) object {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return object{}
}
func arr(v any) []any  { a, _ := v.([]any); return a }
func str(v any) string { s, _ := v.(string); return s }
func fallback(vs ...any) any {
	for _, v := range vs {
		if v != nil && v != "" {
			return v
		}
	}
	return nil
}
func dump(v any) string {
	var b bytes.Buffer
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	if e.Encode(v) != nil {
		return ""
	}
	return strings.TrimSuffix(b.String(), "\n")
}
func decode(b []byte) any {
	var v any
	if json.Unmarshal(b, &v) != nil {
		return nil
	}
	return v
}
func put(a object, k string, v any) {
	if v == nil {
		return
	}
	if s, ok := v.(string); ok && s == "" {
		return
	}
	switch x := v.(type) {
	case []string:
		if len(x) == 0 {
			return
		}
	case []any:
		if len(x) == 0 {
			return
		}
		v = dump(v)
	case map[string]any:
		v = dump(v)
	}
	a[k] = v
}
func number(v any) bool {
	switch v.(type) {
	case float64, int, int64:
		return true
	}
	return false
}
func integer(v any) (int64, bool) {
	switch n := v.(type) {
	case float64:
		if math.IsNaN(n) || math.IsInf(n, 0) {
			return 0, false
		}
		return int64(n), true
	case int64:
		return n, true
	case int:
		return int64(n), true
	}
	return 0, false
}
func text(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	if m, ok := v.(map[string]any); ok {
		if s, ok := m["text"].(string); ok {
			return s
		}
		return text(m["parts"])
	}
	ss := []string{}
	for _, p := range arr(v) {
		if s, ok := p.(string); ok {
			ss = append(ss, s)
		} else {
			m := obj(p)
			if contains([]string{"", "text", "input_text", "output_text"}, str(m["type"])) {
				if s, ok := m["text"].(string); ok {
					ss = append(ss, s)
				}
			}
		}
	}
	return strings.Join(ss, "\n")
}

type call struct{ ID, Name, Args string }
type message struct {
	Role, Content, ToolID string
	Calls                 []call
}
type parsed struct {
	RequestModel, ResponseModel, ID      string
	Reasons                              []string
	In, Out                              []message
	Tools                                []any
	Params, Extra                        object
	Usage                                map[string]int64
	rawUsage                             object
	usageFamily                          string
	Input, InputMIME, Output, OutputMIME string
	HasInput, HasOutput, Skip            bool
}

func newParsed() *parsed {
	return &parsed{Params: object{}, Extra: object{}, Usage: map[string]int64{}}
}
func args(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return dump(v)
}
func (p *parsed) usage(k string, v any) {
	if n, ok := integer(v); ok {
		p.Usage[k] = n
	}
}
func (p *parsed) input(v, ct string)  { p.Input = v; p.InputMIME = ct; p.HasInput = true }
func (p *parsed) output(v, ct string) { p.Output = v; p.OutputMIME = ct; p.HasOutput = true }
func messages(a object, prefix string, ms []message) {
	for i, m := range ms {
		b := fmt.Sprintf("%s.%d.message", prefix, i)
		put(a, b+".role", m.Role)
		put(a, b+".content", m.Content)
		put(a, b+".tool_call_id", m.ToolID)
		for j, c := range m.Calls {
			t := fmt.Sprintf("%s.tool_calls.%d.tool_call", b, j)
			put(a, t+".id", c.ID)
			put(a, t+".function.name", c.Name)
			put(a, t+".function.arguments", c.Args)
		}
	}
}

var contentKeys = strings.Fields("messages input prompt contents system systemInstruction system_instruction instructions tools documents text texts inputs query transcript file audio_url url file_url image_url chat_history message preamble content requests")

func parameters(b object) object {
	p := object{}
	for k, v := range b {
		if !contains(contentKeys, k) {
			p[k] = v
		}
	}
	return p
}
func Build(m *Match, req Request, resp Response) (result *Result) {
	// A malformed provider payload is not an application failure.
	defer func() {
		if recover() != nil {
			result = nil
		}
	}()
	if m == nil {
		return nil
	}
	limit := req.MaxBodyBytes
	if limit <= 0 {
		limit = DefaultMaxBodyBytes
	}
	reqEncoding := req.UndecodedEncoding
	if req.Fields == nil && req.Body != nil && strings.HasPrefix(ContentType(req.Header), "multipart/") {
		var truncated bool
		req.Fields, req.Files, truncated, reqEncoding = ReadEncodedMultipart(bytes.NewReader(req.Body), req.Header.Get("Content-Type"), req.Header.Get("Content-Encoding"), limit)
		req.Truncated = req.Truncated || truncated
		req.Unavailable = req.Unavailable || reqEncoding != ""
		req.Body = nil
	}
	if len(req.Body) > 0 {
		req.Body, req.Truncated, reqEncoding = decodedBytes(req.Body, req.Header.Get("Content-Encoding"), limit, req.Truncated)
		if reqEncoding != "" {
			req.Body = nil
			req.Unavailable = true
		}
	}
	p := check(m, req)
	if p == nil {
		return nil
	}
	respEncoding := ""
	if !Binary(ContentType(resp.Header)) && len(resp.Body) > 0 {
		resp.Body, resp.Truncated, respEncoding = decodedBytes(resp.Body, resp.Header.Get("Content-Encoding"), limit, resp.Truncated)
	}
	b := decodeBody(resp)
	// Explicit transcription text formats are opaque spoken text, not JSON or
	// NDJSON. Numeric and JSON-looking utterances must survive verbatim.
	if m.Endpoint.Format == "openai.audio_transcription" && b.Kind != "sse" && b.Kind != "binary" && resp.Body != nil && !resp.Truncated && respEncoding == "" && (ContentType(resp.Header) == "text/plain" || contains([]string{"text", "srt", "vtt"}, req.Fields["response_format"])) {
		b = body{Kind: "text", Text: string(resp.Body)}
	}
	if m.Endpoint.OnlyWhen == "completed" && resp.Error == nil {
		status := str(obj(b.JSON)["status"])
		if status != "completed" && status != "error" {
			return nil
		}
	}
	ok := resp.Error == nil && resp.Status < 400
	// A read error does not discard complete events received before the error.
	if resp.Status > 0 && resp.Status < 400 {
		parseResponse(m, p, b)
		if p.Skip && resp.Error == nil {
			return nil
		}
	} else if m.Endpoint.OnlyWhen == "completed" && resp.Error == nil {
		return nil
	}
	a := object{"gen_ai.span.kind": m.Endpoint.SpanKind, "gen_ai.operation.name": m.Endpoint.Operation, "gen_ai.provider.name": m.Provider, "traceai.capture.layer": "wire", "traceai.wire.endpoint": m.Endpoint.ID, "server.address": m.Host, "http.request.method": strings.ToUpper(req.Method)}
	if m.Port != 0 {
		a["server.port"] = m.Port
	}
	if resp.Status != 0 {
		a["http.response.status_code"] = resp.Status
	}
	put(a, "gen_ai.request.model", p.RequestModel)
	put(a, "gen_ai.response.model", p.ResponseModel)
	put(a, "gen_ai.response.id", p.ID)
	put(a, "gen_ai.response.finish_reasons", p.Reasons)
	for _, k := range strings.Fields("temperature top_p max_tokens seed frequency_penalty presence_penalty") {
		if v, yes := p.Params[k]; yes {
			a["gen_ai.request."+k] = v
		}
	}
	if v, yes := p.Params["_json"]; yes {
		put(a, "gen_ai.request.parameters", v)
	}
	streamEndpoint := false
	if u, err := url.Parse(req.URL); err == nil {
		for _, suffix := range []string{":streamGenerateContent", ":streamRawPredict", "/converse-stream", "/invoke-with-response-stream"} {
			streamEndpoint = streamEndpoint || strings.HasSuffix(u.Path, suffix)
		}
	}
	// Event-stream provenance comes from the framing, so a zero-byte or
	// truncated SSE/NDJSON stream is still a stream.
	if b.Kind == "sse" || b.Kind == "ndjson" || eventStream(ContentType(resp.Header)) || streamEndpoint {
		a["gen_ai.request.stream"] = true
		encoded := resp.Header.Get("Content-Encoding") != "" && !strings.EqualFold(strings.TrimSpace(resp.Header.Get("Content-Encoding")), "identity")
		for k, v := range streamMilestones(resp, b, encoded) {
			a[k] = v
		}
	}
	// A8: merge cumulative snapshots before normalizing inclusive usage once.
	p.Usage = p.normalizedUsage()
	keys := map[string]string{"input": "input_tokens", "output": "output_tokens", "total": "total_tokens", "provider_total": "provider_total_tokens", "cache_read": "cache_read_tokens", "cache_write": "cache_write_tokens", "reasoning": "output_tokens.reasoning", "input_audio": "input_tokens.audio", "output_audio": "output_tokens.audio"}
	for k, v := range p.Usage {
		a["gen_ai.usage."+keys[k]] = v
	}
	messages(a, "gen_ai.input.messages", p.In)
	messages(a, "gen_ai.output.messages", p.Out)
	for i, t := range p.Tools {
		a[fmt.Sprintf("gen_ai.tool.definitions.%d.tool.json_schema", i)] = dump(t)
	}
	if p.HasInput {
		a["input.value"] = p.Input
		a["input.mime_type"] = p.InputMIME
	}
	if !p.HasOutput && len(p.Out) > 0 {
		msg := p.Out[0]
		if msg.Content != "" {
			p.output(msg.Content, "text/plain")
		} else if len(msg.Calls) > 0 {
			cs := []any{}
			for _, c := range msg.Calls {
				var id, name, arguments any
				if c.ID != "" {
					id = c.ID
				}
				if c.Name != "" {
					name = c.Name
				}
				if c.Args != "" {
					arguments = c.Args
				}
				cs = append(cs, object{"id": id, "name": name, "arguments": arguments})
			}
			p.output(dump(cs), "application/json")
		}
	}
	if p.HasOutput {
		a["output.value"] = p.Output
		a["output.mime_type"] = p.OutputMIME
	}
	for k, v := range p.Extra {
		put(a, k, v)
	}
	if b.Kind == "binary" && ok {
		if _, yes := a["gen_ai.audio.mime_type"]; !yes {
			a["gen_ai.audio.mime_type"] = ContentType(resp.Header)
		}
		if strings.HasPrefix(ContentType(resp.Header), "audio/") || m.Endpoint.Operation == "text_to_speech" {
			a["traceai.audio.output_bytes"] = resp.Size
		}
	}
	if req.Truncated || resp.Truncated {
		a["traceai.wire.truncated"] = true
	}
	if req.Unavailable {
		a["traceai.wire.body_unavailable"] = true
	}
	if resp.Incomplete {
		a["traceai.wire.incomplete"] = true
	}
	put(a, "traceai.wire.undecoded_encoding", fallback(respEncoding, reqEncoding))
	name := m.Endpoint.Operation
	model := p.RequestModel
	if model == "" {
		model = p.ResponseModel
	}
	if model != "" {
		name += " " + model
	}
	desc := ""
	if !ok {
		if resp.Error != nil {
			a["error.type"] = fmt.Sprintf("%T", resp.Error)
			desc = "transport error"
		} else {
			a["error.type"] = strconv.Itoa(resp.Status)
			desc = errorMessage(b)
			if desc == "" {
				desc = fmt.Sprintf("HTTP %d", resp.Status)
			}
		}
	}
	result = &Result{name, a, ok, desc}
	scrubResult(result, req)
	return result
}

type body struct {
	Kind   string
	JSON   any
	Events []object
	Text   string
	// frames are the complete observed frames with their end offsets, in
	// order; A8 usage and A10 milestones consume only these.
	frames []frame
}

func ndjsonType(ct string) bool {
	return ct == "application/x-ndjson" || ct == "application/jsonl" || ct == "application/ndjson"
}
func eventStream(ct string) bool { return ct == "text/event-stream" || ndjsonType(ct) }

func decodeBody(r Response) body {
	ct := ContentType(r.Header)
	if Binary(ct) {
		return body{Kind: "binary"}
	}
	if r.Body == nil || r.Truncated {
		return body{Kind: "empty"}
	}
	if ct == "text/event-stream" {
		// A complete JSON payload is not an observed SSE event until its blank
		// line arrives. EOF, cancellation and failures never complete a frame.
		frames := sseFrames(r.Body)
		return body{Kind: "sse", Events: eventsOf(frames), frames: frames}
	}
	if ndjsonType(ct) {
		frames := ndjsonFrames(r.Body)
		return body{Kind: "ndjson", Events: eventsOf(frames), frames: frames}
	}
	value, frames, array, complete := jsonFrames(r.Body)
	if complete && value != nil {
		return body{Kind: "json", JSON: value, frames: frames}
	}
	if array && len(frames) > 0 {
		// A JSON-array stream that failed/closed after complete members:
		// keep those frames, never fields from a partial trailing member.
		return body{Kind: "json", JSON: value, frames: frames}
	}
	// Some servers stream NDJSON under a JSON content type. The same newline
	// rule applies: an unterminated final line is never an observed record.
	if frames := ndjsonFrames(r.Body); len(eventsOf(frames)) > 0 {
		return body{Kind: "ndjson", Events: eventsOf(frames), frames: frames}
	}
	return body{Kind: "text", Text: string(r.Body)}
}

func errorMessage(b body) string {
	v := b.JSON
	if a, ok := v.([]any); ok && len(a) > 0 {
		v = a[0]
	}
	m := obj(v)
	for _, v := range []any{obj(m["error"])["message"], m["error"], m["message"], m["detail"], m["err_msg"], obj(m["detail"])["message"]} {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	if b.Kind == "text" {
		s := b.Text
		if len(s) > 500 {
			s = s[:500]
		}
		return s
	}
	return ""
}
