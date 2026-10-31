package wire

import (
	"encoding/json"
	"net/http"
	"strings"
)

const CredentialRedacted = "<redacted>"

// HeaderCredentialValues also serves native httptrace callbacks, which see the
// actual headers after auth RoundTrippers have cloned/modified a request.
func HeaderCredentialValues(name string, values []string) []string {
	if !CredentialAttributeKey(name) {
		return nil
	}
	var secrets []string
	for _, value := range values {
		value = strings.TrimSpace(value)
		secrets = append(secrets, value)
		if strings.HasSuffix(privacyLeaf(name), "authorization") {
			if fields := strings.Fields(value); len(fields) > 1 {
				_, token, _ := strings.Cut(value, fields[0])
				secrets = append(secrets, strings.TrimSpace(token))
			}
		}
	}
	return secrets
}

// CredentialValues collects only values for scrubbing, never for export. Header
// names are compared without relying on a caller having canonicalized its map.
func CredentialValues(h http.Header, rawURL string) []string {
	var secrets []string
	for k, values := range h {
		secrets = append(secrets, HeaderCredentialValues(k, values)...)
	}
	return append(secrets, querySecrets(rawURL)...)
}

type privacyBudget struct {
	nodes, bytes int
	mask         func(string) string
}

func newPrivacyBudget() *privacyBudget {
	return &privacyBudget{nodes: privacyRules.Limits.Nodes, bytes: privacyRules.Limits.Text}
}
func (b *privacyBudget) text(value string, secrets []string, framed bool) string {
	value = privacyText(value, secrets, framed)
	if b.mask != nil {
		value = b.mask(value)
	}
	return value
}
func (b *privacyBudget) take(bytes, depth int) bool {
	b.nodes--
	b.bytes -= bytes
	return b.nodes >= 0 && b.bytes >= 0 && depth <= privacyRules.Limits.Depth
}

// PrivacyAttributeString also handles values whose structural key is provided
// separately by OpenTelemetry rather than inside a serialized JSON object.
func PrivacyAttributeString(key, value string, secrets []string) string {
	return PrivacyValue(value, key, nil, secrets).(string)
}
func PrivacyAttributeStringWithMasker(key, value string, mask func(string) string) string {
	return PrivacyValueWithMasker(value, key, nil, mask).(string)
}

// PrivacyString is shared by core attributes, status descriptions and OTel
// context attributes. Untouched JSON retains its original whitespace.
func PrivacyString(s string, secrets []string) string {
	return PrivacyValue(s, "", nil, secrets).(string)
}

func jsonSpace(c byte) bool { return c == ' ' || c == '\t' || c == '\r' || c == '\n' }
func stringEnd(s string, start int) int {
	for i := start + 1; i < len(s); i++ {
		if s[i] == '\\' {
			i++
		} else if s[i] == '"' {
			return i + 1
		}
	}
	return len(s)
}
func partialValueEnd(s string, start int) int {
	if start == len(s) {
		return start
	}
	if s[start] == '"' {
		return stringEnd(s, start)
	}
	depth := 0
	for i := start; i < len(s); i++ {
		switch s[i] {
		case '"':
			i = stringEnd(s, i) - 1
		case '{', '[':
			depth++
		case '}', ']':
			if depth == 0 {
				return i
			}
			depth--
			if depth == 0 {
				return i + 1
			}
		case ',':
			if depth == 0 {
				return i
			}
		default:
			if depth == 0 && jsonSpace(s[i]) {
				return i
			}
		}
	}
	return len(s)
}
func partialPrivacy(s string, secrets []string, budget *privacyBudget, depth int) string {
	var out strings.Builder
	last := 0
	for i := 0; i < len(s); i++ {
		if s[i] != '"' {
			continue
		}
		end := stringEnd(s, i)
		var token string
		complete := json.Unmarshal([]byte(s[i:end]), &token) == nil
		colon := end
		for colon < len(s) && jsonSpace(s[colon]) {
			colon++
		}
		key := ""
		start, stop := i, end
		if complete && colon < len(s) && s[colon] == ':' {
			key = token
			start = colon + 1
			for start < len(s) && jsonSpace(s[start]) {
				start++
			}
			if CredentialAttributeKey(key) {
				stop = partialValueEnd(s, start)
				out.WriteString(s[last:start])
				out.WriteString(`"<redacted>"`)
				last = stop
				i = stop - 1
				continue
			}
			if start == len(s) || s[start] != '"' {
				i = end - 1
				continue
			}
			stop = stringEnd(s, start)
		}
		raw := s[start:stop]
		if json.Unmarshal([]byte(raw), &token) != nil && json.Unmarshal([]byte(raw+`"`), &token) != nil {
			break
		}
		if scrubbed := privacyWalk(token, key, secrets, budget, depth+1, mediaHint{}, nil, true).(string); scrubbed != token {
			out.WriteString(s[last:start])
			out.WriteString(dump(scrubbed))
			last = stop
		}
		i = stop - 1
	}
	if last == 0 {
		return s
	}
	out.WriteString(s[last:])
	return out.String()
}

// Only walk already-parsed JSON/native form values. Credential ancestry is kept
// through objects/arrays. Never decode JSON in an opaque string for harvesting,
// stringify arbitrary values, or buffer a body solely for discovery.
func observeCredentials(value any, key string, observe func(string), budget *privacyBudget, depth int) {
	if observe == nil || !budget.take(0, depth) {
		return
	}
	switch value := value.(type) {
	case string:
		for _, secret := range HeaderCredentialValues(key, []string{value}) {
			observe(secret)
		}
	case map[string]any:
		for name, value := range value {
			observeCredentials(value, joinPath(key, name), observe, budget, depth+1)
		}
	case []any:
		for _, value := range value {
			observeCredentials(value, key, observe, budget, depth+1)
		}
	}
}

func scrubResult(r *Result, req Request) {
	var secrets []string
	if req.ObserveSecret == nil {
		secrets = append(append([]string(nil), req.Secrets...), CredentialValues(req.Header, req.URL)...)
	}
	// Structural scrubbing is best-effort upstream of S1. Exhausting the catalog
	// budget must discard content, not unwind into Build's nil-result guard and
	// erase the entire request (raw/decoded/flattened copies share this budget).
	defer func() {
		if recover() != nil {
			r.Name = "traceai span"
			r.Description = ""
			r.Attributes = privacyResultFallback(r.Attributes, secrets)
		}
	}()
	// Value masking for agent exports waits until S1; structural privacy applies
	// here too, without replacing a known prefix of a later-discovered secret.
	r.Name = PrivacyString(r.Name, secrets)
	r.Description = PrivacyString(r.Description, secrets)
	clean := PrivacyValue(r.Attributes, "", nil, secrets).(map[string]any)
	for key, value := range clean {
		if original, ok := r.Attributes[key].([]string); ok {
			if values, ok := value.([]any); ok {
				stringsOut := make([]string, len(original))
				for i, item := range values {
					stringsOut[i], _ = item.(string)
				}
				value = stringsOut
			}
		}
		clean[key] = value
	}
	r.Attributes = clean
}

// Keep generated protocol identity, outcome, usage and degradation metadata, but
// no request/response content or opaque parameter blobs. Even these fields pass
// the catalog: a malformed/oversize identity falls back to fixed provenance only.
func privacyResultFallback(attrs map[string]any, secrets []string) (out map[string]any) {
	out = map[string]any{"traceai.capture.layer": "wire", "traceai.wire.privacy_degraded": true}
	defer func() {
		if recover() != nil {
			out = map[string]any{"traceai.capture.layer": "wire", "traceai.wire.privacy_degraded": true}
		}
	}()
	retained := map[string]any{}
	for key, value := range attrs {
		switch key {
		case "gen_ai.span.kind", "gen_ai.operation.name", "gen_ai.provider.name", "traceai.wire.endpoint", "server.address", "server.port", "http.request.method", "http.response.status_code", "error.type":
			retained[key] = value
		default:
			if strings.HasPrefix(key, "gen_ai.usage.") || strings.HasPrefix(key, "traceai.") {
				switch value.(type) {
				case bool, int, int64, float64:
					retained[key] = value
				}
			}
		}
	}
	clean := PrivacyValue(retained, "", nil, secrets).(map[string]any)
	for key, value := range clean {
		out[key] = value
	}
	return out
}
