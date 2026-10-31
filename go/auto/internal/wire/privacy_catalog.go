package wire

// S6 recognition is loaded once from the shipping, byte-identical catalog.
// Transport harvesting and every export surface use these same predicates.
import (
	_ "embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

//go:embed privacy.json
var PrivacyCatalogJSON []byte

type privacyCatalog struct {
	Normalization struct{ Remove []string }
	Credentials   struct {
		Replacement string
		Patterns    []struct{ ID, Contains string }
		Exclusions  []struct {
			Regex  string
			Unless []string `json:"unless_contains_patterns"`
		}
		Contextual []struct {
			Regex string `json:"path_regex"`
		} `json:"contextual_exclusions"`
	}
	SignedURL struct{ Parameters []string } `json:"signed_url"`
	Media     struct {
		Alphabet    string   `json:"base64_alphabet"`
		PrefixBytes int      `json:"base64_prefix_decoded_bytes"`
		Threshold   int      `json:"unrecognized_base64_min_chars_exclusive"`
		Fields      []string `json:"unrecognized_base64_fields"`
		Replacement string
		Other       string `json:"other_replacement"`
		Magic       []struct {
			Tests []struct {
				Offset    int
				Hex, Mask string
			}
		}
		DataURI struct {
			Other string `json:"other_replacement"`
		} `json:"data_uri"`
		MIME struct {
			Names, Prefixes, Exact []string
			Payload                []string `json:"payload_fields"`
		} `json:"mime_siblings"`
		Audio struct {
			Fields   []string
			Payload  []string `json:"payload_fields"`
			Types    []string `json:"type_names"`
			Metadata []string `json:"metadata_fields"`
		}
	}
	Limits struct {
		Depth int `json:"max_depth"`
		Nodes int `json:"max_nodes"`
		Text  int `json:"max_text_chars"`
	}
}

type nameExclusion struct {
	regex  *regexp.Regexp
	guards []string
}
type magicTest struct {
	offset         int
	expected, mask []byte
}
type compiledPrivacy struct {
	privacyCatalog
	fold       *strings.Replacer
	exclusions []nameExclusion
	contextual []*regexp.Regexp
	magic      [][]magicTest
	signed     map[string]bool
	alphabet   [256]bool
}

var privacyRules = loadPrivacyCatalog()

func loadPrivacyCatalog() *compiledPrivacy {
	p := &compiledPrivacy{}
	if err := json.Unmarshal(PrivacyCatalogJSON, &p.privacyCatalog); err != nil {
		panic("invalid embedded privacy catalog")
	}
	var pairs []string
	for _, char := range p.Normalization.Remove {
		pairs = append(pairs, char, "")
	}
	p.fold = strings.NewReplacer(pairs...)
	patterns := map[string]string{}
	for _, pattern := range p.Credentials.Patterns {
		patterns[pattern.ID] = pattern.Contains
	}
	for _, exclusion := range p.Credentials.Exclusions {
		e := nameExclusion{regex: regexp.MustCompile(exclusion.Regex)}
		for _, id := range exclusion.Unless {
			e.guards = append(e.guards, patterns[id])
		}
		p.exclusions = append(p.exclusions, e)
	}
	for _, exclusion := range p.Credentials.Contextual {
		p.contextual = append(p.contextual, regexp.MustCompile(exclusion.Regex))
	}
	p.signed = map[string]bool{}
	for _, name := range p.SignedURL.Parameters {
		p.signed[p.normalize(name)] = true
	}
	for i := range len(p.Media.Alphabet) {
		p.alphabet[p.Media.Alphabet[i]] = true
	}
	for _, rule := range p.Media.Magic {
		var tests []magicTest
		for _, test := range rule.Tests {
			expected, err := hex.DecodeString(test.Hex)
			if err != nil {
				panic("invalid embedded media magic")
			}
			mask := strings.Repeat("ff", len(expected))
			if test.Mask != "" {
				mask = test.Mask
			}
			decodedMask, err := hex.DecodeString(mask)
			if err != nil || len(decodedMask) != len(expected) {
				panic("invalid embedded media mask")
			}
			tests = append(tests, magicTest{test.Offset, expected, decodedMask})
		}
		p.magic = append(p.magic, tests)
	}
	return p
}

func asciiLower(s string) string {
	return strings.Map(func(c rune) rune {
		if c >= 'A' && c <= 'Z' {
			return c + 'a' - 'A'
		}
		return c
	}, s)
}
func (p *compiledPrivacy) normalize(s string) string { return p.fold.Replace(asciiLower(s)) }
func pathParts(s string) []string {
	return strings.FieldsFunc(s, func(c rune) bool { return c == '.' || c == '[' || c == ']' })
}
func numericPart(s string) bool {
	if s == "" {
		return false
	}
	for i := range len(s) {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}
func privacyLeaf(s string) string {
	parts := pathParts(s)
	for i := len(parts) - 1; i >= 0; i-- {
		if !numericPart(parts[i]) {
			return privacyRules.normalize(parts[i])
		}
	}
	return ""
}
func (p *compiledPrivacy) excluded(s string) bool {
	for _, e := range p.exclusions {
		if !e.regex.MatchString(s) {
			continue
		}
		guarded := false
		for _, marker := range e.guards {
			guarded = guarded || strings.Contains(s, marker)
		}
		if !guarded {
			return true
		}
	}
	return false
}
func (p *compiledPrivacy) pattern(s string) bool {
	for _, pattern := range p.Credentials.Patterns {
		if strings.Contains(s, pattern.Contains) {
			return true
		}
	}
	return false
}

// CredentialAttributeKey is the ONE name predicate for headers, JSON fields,
// query names and native/flattened/indexed attributes. Exclusions are local, not
// subtree exemptions, and a credential ancestor always retains its meaning.
func CredentialAttributeKey(name string) bool {
	p := privacyRules
	var parts []string
	rawParts := pathParts(name)
	if len(rawParts) > p.Limits.Depth {
		return true
	} // Fail closed on over-deep flattened paths too.
	for _, part := range rawParts {
		if !numericPart(part) {
			parts = append(parts, p.normalize(part))
		}
	}
	excluded := false
	for i, part := range parts {
		local := p.excluded(part)
		for _, e := range p.contextual {
			local = local || e.MatchString(strings.Join(parts[:i+1], "."))
		}
		if local {
			excluded = true
		} else if p.pattern(part) {
			return true
		}
	}
	return !excluded && !p.excluded(p.normalize(name)) && p.pattern(p.normalize(name))
}
func queryCredentialKey(name string) bool {
	return privacyRules.signed[privacyRules.normalize(name)] || CredentialAttributeKey(name)
}

func base64Alphabet(s string) bool {
	if s == "" {
		return false
	}
	for i := range len(s) {
		if !privacyRules.alphabet[s[i]] {
			return false
		}
	}
	return true
}
func omitted(s string) string {
	return strings.ReplaceAll(privacyRules.Media.Replacement, "{length}", strconv.Itoa(len(s)))
}
func magicMedia(value string) bool {
	if !base64Alphabet(value) {
		return false
	}
	count := privacyRules.Media.PrefixBytes
	// Only copy/decode the bounded prefix; the alphabet validation above is linear.
	var prefix strings.Builder
	limit := (count*8 + 5) / 6
	for i := 0; i < len(value) && prefix.Len() < limit; i++ {
		if value[i] != '\r' && value[i] != '\n' {
			prefix.WriteByte(value[i])
		}
	}
	s := strings.NewReplacer("-", "+", "_", "/").Replace(prefix.String())
	s += strings.Repeat("=", (4-len(s)%4)%4)
	decoded, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		return false
	}
	if len(decoded) > count {
		decoded = decoded[:count]
	}
	for _, rule := range privacyRules.magic {
		matched := true
		for _, test := range rule {
			if test.offset+len(test.expected) > len(decoded) {
				matched = false
				break
			}
			for i, expected := range test.expected {
				if decoded[test.offset+i]&test.mask[i] != expected&test.mask[i] {
					matched = false
					break
				}
			}
			if !matched {
				break
			}
		}
		if matched {
			return true
		}
	}
	return false
}

type mediaHint struct{ media, audio bool }
type privacyHints map[string]mediaHint

func combineHint(a, b mediaHint) mediaHint { return mediaHint{a.media || b.media, a.audio || b.audio} }
func hint(name string, value any) mediaHint {
	if values, ok := value.([]any); ok {
		var result mediaHint
		for _, value := range values {
			if _, ok := value.(string); ok {
				result = combineHint(result, hint(name, value))
			}
		}
		return result
	}
	if values, ok := value.([]string); ok {
		var result mediaHint
		for _, value := range values {
			result = combineHint(result, hint(name, value))
		}
		return result
	}
	text, ok := value.(string)
	if !ok {
		return mediaHint{}
	}
	leaf := privacyLeaf(name)
	mime, _, _ := strings.Cut(text, ";")
	mime = asciiLower(strings.TrimSpace(mime))
	if contains(privacyRules.Media.MIME.Names, leaf) {
		media := contains(privacyRules.Media.MIME.Exact, mime)
		for _, prefix := range privacyRules.Media.MIME.Prefixes {
			media = media || strings.HasPrefix(mime, prefix)
		}
		return mediaHint{media, strings.HasPrefix(mime, "audio/")}
	}
	if leaf == "type" && contains(privacyRules.Media.Audio.Types, privacyRules.normalize(text)) {
		return mediaHint{audio: true}
	}
	return mediaHint{}
}

// MediaHintParent reports whether a flattened attribute is a catalog MIME/type
// hint (media or audio) and returns the parent path whose payload fields it
// governs. Projection keeps such a hint with every admitted key under parent.
func MediaHintParent(key string, value any) (string, bool) {
	if h := hint(key, value); h.media || h.audio {
		return hintParent(key), true
	}
	return "", false
}
func hintParent(path string) string {
	parts := pathParts(path)
	for len(parts) > 0 && numericPart(parts[len(parts)-1]) {
		parts = parts[:len(parts)-1]
	}
	if len(parts) == 0 {
		return ""
	}
	return strings.Join(parts[:len(parts)-1], ".")
}
func joinPath(path, key string) string {
	if path == "" {
		return key
	}
	return path + "." + key
}

// visitPrivacyHints indexes canonical paths across native, flattened, indexed
// and JSON-encoded objects before any payload is walked. Both raw-media and the
// optional hide-images policy use this same bounded, representation-neutral pass.
func visitPrivacyHints(value any, path string, budget *privacyBudget, depth int, visit func(string, any)) {
	if !budget.take(0, depth) {
		panic("traceai privacy hint budget")
	}
	switch x := value.(type) {
	case map[string]any:
		if len(x) > budget.nodes {
			panic("traceai privacy hint budget")
		}
		for name, item := range x {
			if !budget.take(len(name), depth) {
				panic("traceai privacy hint budget")
			}
			visitPrivacyHints(item, joinPath(path, name), budget, depth+1, visit)
		}
	case []any:
		if len(x) > budget.nodes {
			panic("traceai privacy hint budget")
		}
		for i, item := range x {
			visitPrivacyHints(item, joinPath(path, strconv.Itoa(i)), budget, depth+1, visit)
		}
	case []string:
		if len(x) > budget.nodes {
			panic("traceai privacy hint budget")
		}
		for i, item := range x {
			visitPrivacyHints(item, joinPath(path, strconv.Itoa(i)), budget, depth+1, visit)
		}
	case string:
		if !budget.take(len(x), depth) {
			panic("traceai privacy hint budget")
		}
		visit(path, x)
		trim := strings.TrimSpace(x)
		if strings.HasPrefix(trim, "{") || strings.HasPrefix(trim, "[") || strings.HasPrefix(trim, `"`) {
			var decoded any
			if json.Unmarshal([]byte(x), &decoded) == nil {
				visitPrivacyHints(decoded, path, budget, depth+1, visit)
			}
		}
	}
}
func indexHints(value any, path string, inherited privacyHints) privacyHints {
	hints := make(privacyHints, len(inherited))
	for k, h := range inherited {
		hints[k] = h
	}
	visitPrivacyHints(value, path, newPrivacyBudget(), 0, func(path string, value any) {
		if h := hint(path, value); h.media || h.audio {
			parent := hintParent(path)
			hints[parent] = combineHint(hints[parent], h)
		}
	})
	return hints
}
func mediaContext(path string, inherited mediaHint, hints privacyHints) (mediaHint, bool) {
	context := combineHint(inherited, hints[""])
	payload, directAudio := false, false
	var prefix []string
	for _, part := range pathParts(path) {
		prefix = append(prefix, part)
		name := privacyRules.normalize(part)
		if !numericPart(part) {
			if contains(privacyRules.Media.Audio.Metadata, name) {
				context = mediaHint{}
				payload = false
			}
			directAudio = contains(privacyRules.Media.Audio.Fields, name)
			context.audio = context.audio || directAudio
			if context.media && contains(privacyRules.Media.MIME.Payload, name) || context.audio && contains(privacyRules.Media.Audio.Payload, name) {
				payload = true
			}
		}
		context = combineHint(context, hints[strings.Join(prefix, ".")])
	}
	return context, payload || directAudio
}

// mediaString is the ONE structural media predicate. It is also used for decoded
// URL values, never just for fields with a familiar leaf name.
func mediaString(value, path string, inherited mediaHint, hints privacyHints, framed bool) (string, bool) {
	if stableReplacement(value) {
		return "", false
	}
	if framed {
		if replacement, ok := dataURI(value); ok {
			return replacement, true
		}
	}
	encoded := base64Alphabet(value)
	_, payload := mediaContext(path, inherited, hints)
	if magicMedia(value) || payload {
		if encoded {
			return omitted(value), true
		}
		return privacyRules.Media.Other, true
	}
	if encoded && contains(privacyRules.Media.Fields, privacyLeaf(path)) && len(value) > privacyRules.Media.Threshold {
		return omitted(value), true
	}
	return "", false
}

var omissionMarker = regexp.MustCompile(`^<omitted: (?:[0-9]+ base64 chars|media|data URI)>$`)

func stableReplacement(value string) bool {
	return value == CredentialRedacted || omissionMarker.MatchString(value)
}

// ImageHint is used only by the optional hide-images policy, not by raw-media
// recognition (which is always on). MIME field names still come from the catalog.
func ImageHint(name, value string) bool {
	mime, _, _ := strings.Cut(value, ";")
	return contains(privacyRules.Media.MIME.Names, privacyLeaf(name)) && strings.HasPrefix(asciiLower(strings.TrimSpace(mime)), "image/")
}

// PrivacyValue sanitizes JSON-compatible typed values on a detached copy. The
// same walk is used for parsed bodies and native OTel trees. Unknown values are
// represented by their type without invoking String/MarshalJSON/user code.
func PrivacyValue(value any, path string, siblings map[string]any, secrets []string) any {
	return privacyWalk(value, path, secrets, newPrivacyBudget(), 0, mediaHint{}, indexHints(siblings, "", nil), true)
}
func PrivacyValueWithMasker(value any, path string, siblings map[string]any, mask func(string) string) any {
	budget := newPrivacyBudget()
	budget.mask = mask
	return privacyWalk(value, path, nil, budget, 0, mediaHint{}, indexHints(siblings, "", nil), true)
}
func privacyWalk(value any, path string, secrets []string, budget *privacyBudget, depth int, inherited mediaHint, hints privacyHints, framed bool) any {
	if depth == 0 {
		hints = indexHints(value, path, hints)
	}
	if !budget.take(0, depth) {
		panic("traceai privacy structure budget")
	}
	if CredentialAttributeKey(path) {
		return CredentialRedacted
	}
	switch x := value.(type) {
	case map[string]any:
		if len(x) > budget.nodes {
			panic("traceai privacy structure budget")
		}
		out := make(map[string]any, len(x))
		for key, value := range x {
			if !budget.take(len(key), depth) {
				panic("traceai privacy text budget")
			}
			out[key] = privacyWalk(value, joinPath(path, key), secrets, budget, depth+1, inherited, hints, true)
		}
		return out
	case []string:
		values := make([]any, len(x))
		for i, value := range x {
			values[i] = value
		}
		return privacyWalk(values, path, secrets, budget, depth+1, inherited, hints, framed)
	case []any:
		if len(x) > budget.nodes {
			panic("traceai privacy structure budget")
		}
		_, payload := mediaContext(path, inherited, hints)
		if payload && len(x) > 0 {
			numeric := true
			for _, value := range x {
				switch value.(type) {
				case bool, float64, int, int64, json.Number:
				default:
					numeric = false
				}
			}
			if numeric {
				return privacyRules.Media.Other
			}
		}
		out := make([]any, len(x))
		for i, value := range x {
			out[i] = privacyWalk(value, joinPath(path, strconv.Itoa(i)), secrets, budget, depth+1, inherited, hints, true)
		}
		return out
	case string:
		if !budget.take(len(x), depth) {
			panic("traceai privacy text budget")
		}
		if replacement, ok := mediaString(x, path, inherited, hints, framed); ok {
			return replacement
		}
		trim := strings.TrimSpace(x)
		if strings.HasPrefix(trim, "{") || strings.HasPrefix(trim, "[") {
			var parsed any
			if json.Unmarshal([]byte(x), &parsed) == nil {
				context, _ := mediaContext(path, inherited, hints)
				clean := privacyWalk(parsed, path, secrets, budget, depth+1, context, hints, true)
				if dump(parsed) != dump(clean) {
					x = dump(clean)
				}
				return budget.text(x, secrets, framed)
			}
		}
		return budget.text(partialPrivacy(x, secrets, budget, depth+1), secrets, framed)
	case nil:
		return nil
	case bool, float64, int, int64, json.Number:
		_, payload := mediaContext(path, inherited, hints)
		if payload {
			return privacyRules.Media.Other
		}
		return x
	default:
		return fmt.Sprintf("%T", value)
	}
}
