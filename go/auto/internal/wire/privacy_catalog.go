package wire

// S6 recognition is loaded once from the shipping, byte-identical catalog.
// Transport harvesting and every export surface use these same predicates.
import (
	_ "embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
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
		Shapes []struct{ Regex, Flags string } `json:"value_shapes"`
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
		Image imageRule
		Video imageRule
	}
	Limits struct {
		Depth int `json:"max_depth"`
		Nodes int `json:"max_nodes"`
		Text  int `json:"max_text_chars"`
	}
	ScalarMetrics scalarMetricCatalog `json:"scalar_metrics"`
}

// scalarMetricCatalog is the closed set of exact usage and metric keys whose
// scalar values are metadata, even when the key spells audio or bytes.
type scalarMetricCatalog struct {
	Decimal    decimalCountRule `json:"canonical_decimal"`
	Containers []metricContainer
}
type decimalCountRule struct {
	Regex    string
	MaxChars int `json:"max_chars"`
	MinChars int `json:"base64_min_chars"`
	Keys     []string
}
type metricContainer struct {
	Keys      []string
	ValueType string `json:"value_type"`
}

// imageRule is a catalog image or video declaration. Video has no payload
// fields of its own; it uses the generic MIME payload fields.
type imageRule struct {
	Types    []string `json:"type_names"`
	Modality []string `json:"modality_names"`
	Payload  []string `json:"payload_fields"`
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
	shapes     []*regexp.Regexp
	magic      [][]magicTest
	signed     map[string]bool
	alphabet   [256]bool
	metrics    map[string]bool
	counts     map[string]bool
	decimals   map[string]bool
	decimal    *regexp.Regexp
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
	for _, shape := range p.Credentials.Shapes {
		flags := ""
		for _, flag := range shape.Flags {
			if !strings.ContainsRune("ims", flag) {
				panic("invalid embedded credential value shape")
			}
			flags += string(flag)
		}
		if flags != "" {
			flags = "(?" + flags + ")"
		}
		p.shapes = append(p.shapes, regexp.MustCompile(flags+shape.Regex))
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
	p.metrics, p.counts, p.decimals = map[string]bool{}, map[string]bool{}, map[string]bool{}
	for _, container := range p.ScalarMetrics.Containers {
		if container.ValueType != "" && container.ValueType != "nonnegative_safe_integer" {
			panic("invalid embedded scalar metric type")
		}
		for _, key := range container.Keys {
			p.metrics[key] = true
			if container.ValueType != "" {
				p.counts[key] = true
			}
		}
	}
	for _, key := range p.ScalarMetrics.Decimal.Keys {
		p.decimals[key] = true
	}
	p.decimal = regexp.MustCompile(p.ScalarMetrics.Decimal.Regex)
	return p
}

const maxSafeInteger = 1<<53 - 1

// scalarMetric reports the catalog's scalar metric role: the exact complete
// key of a declared counter, a valid scalar for that counter, and no explicit
// or inherited media/audio context on the key or any ancestor. The role only
// removes the media classification that the key's own spelling (audio, bytes)
// would cause; magic bytes, data URIs and credential rules still apply.
func scalarMetric(value any, path string, inherited mediaHint, hints privacyHints) bool {
	p := privacyRules
	if !p.metrics[path] || inherited.media || inherited.audio {
		return false
	}
	parts := pathParts(path)
	for i := 0; i <= len(parts); i++ {
		if h := hints[strings.Join(parts[:i], ".")]; h.media || h.audio {
			return false
		}
	}
	if !p.counts[path] {
		switch value.(type) {
		case bool, float64, int, int64, json.Number, string:
			return true
		}
		return false
	}
	switch x := value.(type) {
	case float64:
		return x >= 0 && x <= maxSafeInteger && x == math.Trunc(x)
	case int:
		return x >= 0 && int64(x) <= maxSafeInteger
	case int64:
		return x >= 0 && x <= maxSafeInteger
	case json.Number:
		n, err := x.Int64()
		return err == nil && n >= 0 && n <= maxSafeInteger
	case string:
		// Only the declared byte counter keeps a canonical decimal string, and
		// never one with base64 shape at or above the catalog's length.
		rule := p.ScalarMetrics.Decimal
		if !p.decimals[path] || x == "" || len(x) > rule.MaxChars || p.decimal.FindString(x) != x || len(x) >= rule.MinChars && base64Alphabet(x) {
			return false
		}
		n, err := strconv.ParseInt(x, 10, 64)
		return err == nil && n <= maxSafeInteger
	}
	return false
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

// redactValueShapes replaces every catalog credential value shape (provider
// keys, access key IDs, JWTs, authorization values, private-key PEM) in text,
// whatever key the text sits under. The replacement marker matches no shape.
func redactValueShapes(s string) string {
	for _, shape := range privacyRules.shapes {
		s = shape.ReplaceAllLiteralString(s, CredentialRedacted)
	}
	return s
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
	s := strings.TrimRight(strings.NewReplacer("-", "+", "_", "/").Replace(prefix.String()), "=")
	if len(s)%4 == 1 {
		s = s[:len(s)-1] // The trailing partial quantum cannot invalidate earlier magic.
	}
	decoded, err := base64.RawStdEncoding.DecodeString(s)
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

// mediaHint is the context a MIME/type/modality hint gives its parent object.
// image is extra image evidence: it can only widen omission, never admit text.
type mediaHint struct{ media, audio, image bool }
type privacyHints map[string]mediaHint

func combineHint(a, b mediaHint) mediaHint {
	return mediaHint{a.media || b.media, a.audio || b.audio, a.image || b.image}
}
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
		return mediaHint{media, strings.HasPrefix(mime, "audio/"), strings.HasPrefix(mime, "image/")}
	}
	switch leaf {
	case "type":
		// Image and video types establish generic media context; audio types
		// establish audio context only.
		kind := privacyRules.normalize(mime)
		image := contains(privacyRules.Media.Image.Types, kind)
		video := contains(privacyRules.Media.Video.Types, kind)
		return mediaHint{image || video, contains(privacyRules.Media.Audio.Types, privacyRules.normalize(text)), image}
	case "modality":
		image := contains(privacyRules.Media.Image.Modality, mime)
		return mediaHint{image || contains(privacyRules.Media.Video.Modality, mime), false, image}
	}
	return mediaHint{}
}

// declaredImagePayload reports a path under an image declaration (image type,
// modality or MIME) whose component is a catalog image payload field. Audio
// metadata components end the declaration unless they are image payload
// fields themselves (url), and a fresh image hint below starts it again.
// Callers omit only encoded values, so ordinary HTTP image URLs stay text.
func declaredImagePayload(path string, hints privacyHints) bool {
	image, payload := hints[""].image, false
	var prefix []string
	for _, part := range pathParts(path) {
		prefix = append(prefix, part)
		if !numericPart(part) {
			name := privacyRules.normalize(part)
			field := contains(privacyRules.Media.Image.Payload, name)
			if contains(privacyRules.Media.Audio.Metadata, name) && !field {
				image, payload = false, false
			}
			if image && field {
				payload = true
			}
		}
		image = image || hints[strings.Join(prefix, ".")].image
	}
	return payload
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

// MediaHintParents is MediaHintParent for every representation: it applies
// the same hint predicate to each string inside a native map, generic slice,
// string array or JSON-encoded value (the indexHints visitor) and returns the
// governed parent paths, sorted and unique. A value too large to index fails
// closed to the root parent "", which governs every key.
func MediaHintParents(key string, value any) (parents []string) {
	defer func() {
		if recover() != nil {
			parents = []string{""}
		}
	}()
	seen := map[string]bool{}
	visitPrivacyHints(value, key, newPrivacyBudget(), 0, func(path string, item any) {
		if h := hint(path, item); h.media || h.audio {
			seen[hintParent(path)] = true
		}
	})
	for parent := range seen {
		parents = append(parents, parent)
	}
	sort.Strings(parents)
	return parents
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
	payload = payload && !scalarMetric(value, path, inherited, hints)
	if magicMedia(value) || payload {
		if encoded {
			return omitted(value), true
		}
		return privacyRules.Media.Other, true
	}
	if encoded && declaredImagePayload(path, hints) {
		return omitted(value), true
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

// MediaRisk describes an attribute object whose MIME/type context may be
// incomplete: an SDK count limit dropped attributes, a value-length limit cut
// a value (a truncated MIME is never a safe MIME), or a budget fallback
// discarded siblings.
type MediaRisk struct {
	// Scopes are the hint parents (MediaRiskScope) whose MIME/type context may
	// be incomplete. Nil means every path (dropped attributes, fallbacks).
	Scopes []string
	// Truncated reports a string a value-length limit may have cut; nil when
	// the provider has no such limit. Cut strings and decoded descendants use
	// the lexical fail-closed boundary regardless of their key or MIME scope.
	Truncated func(string) bool
}

// MediaRiskScope is the subtree whose MIME/type context a possibly-truncated
// attribute key may have carried: the hint parent of the key itself (a MIME or
// type value governs its parent object; a JSON/map value governs its own
// subtree, which the parent subtree contains).
func MediaRiskScope(key string) string { return hintParent(key) }

// PrivacyValueFailClosed is PrivacyValueWithMasker for an attribute object
// whose MIME/type context may be incomplete. Recognition no longer depends on
// the (possibly evicted or cut) hints: inside an at-risk scope EVERY non-empty
// value under a catalog media-capable path is omitted. Truncated strings also
// omit every 8+ base64 run and data: fragment without reconstructing context;
// shorter prefixes consistent with catalog magic remain protected too.
func PrivacyValueFailClosed(value any, path string, siblings map[string]any, mask func(string) string, risk MediaRisk) any {
	budget := newPrivacyBudget()
	budget.mask = mask
	budget.degraded = true
	budget.risk = risk
	return privacyWalk(value, path, nil, budget, 0, mediaHint{}, indexHints(siblings, "", nil), true)
}

// atRisk reports a path inside a scope whose MIME/type context may be lost.
// Paths are compared in their canonical component form (brackets folded).
func (b *privacyBudget) atRisk(path string) bool {
	if !b.degraded {
		return false
	}
	if b.risk.Scopes == nil {
		return true
	}
	canonical := strings.Join(pathParts(path), ".")
	for _, scope := range b.risk.Scopes {
		if scope == "" || canonical == scope || strings.HasPrefix(canonical, scope+".") {
			return true
		}
	}
	return false
}

// failClosedMediaPath is the fail-closed path predicate, built only from the
// catalog: a non-numeric component naming a MIME payload field, an audio field,
// an audio payload field or an unrecognized-base64 field makes the path and all
// its indexed/flattened descendants media-capable. An audio metadata component
// (transcript, id, text, ...) ends that capability, but a capable component
// beneath it starts it again, exactly as a fresh hint may below a boundary. The
// catalog's payload rules can only mark a path whose components satisfy this
// predicate, so it holds without any (possibly evicted) MIME/type sibling.
func failClosedMediaPath(path string) bool {
	capable := false
	for _, part := range pathParts(path) {
		if numericPart(part) {
			continue
		}
		name := privacyRules.normalize(part)
		switch {
		case contains(privacyRules.Media.Audio.Metadata, name):
			capable = false
		case contains(privacyRules.Media.MIME.Payload, name), contains(privacyRules.Media.Audio.Fields, name), contains(privacyRules.Media.Audio.Payload, name), contains(privacyRules.Media.Fields, name):
			capable = true
		}
	}
	return capable
}

// magicPrefix reports a non-empty base64 string whose decodable prefix
// is consistent with a catalog magic rule: some continuation of it could
// satisfy the rule, i.e. every covered byte of every test matches under its
// mask. A prefix that ends before a rule's first tested byte (ftyp's four
// arbitrary box-size bytes, often all that survives once CR/LF wrapping uses
// the length budget) or that carries data characters but no whole byte is
// consistent too, so every non-empty payload prefix of a magicMedia value is
// recognized. Used only for values a length limit may have cut (a cut
// PNG/WAV/MP4 prefix keeps no full signature). Bounded to the catalog's
// prefix length.
func magicPrefix(value string) bool {
	if !base64Alphabet(value) {
		return false
	}
	count := privacyRules.Media.PrefixBytes
	limit := (count*8 + 5) / 6
	var prefix strings.Builder
	for i := 0; i < len(value) && prefix.Len() < limit; i++ {
		switch c := value[i]; c {
		case '\r', '\n':
		case '=':
			i = len(value)
		case '-':
			prefix.WriteByte('+')
		case '_':
			prefix.WriteByte('/')
		default:
			prefix.WriteByte(c)
		}
	}
	s := prefix.String()
	if s == "" {
		return false // Only CR/LF or padding survived: no payload to leak.
	}
	if len(s)%4 == 1 {
		s = s[:len(s)-1] // A lone trailing character carries no whole byte.
	}
	decoded, err := base64.RawStdEncoding.DecodeString(s)
	if err != nil || len(decoded) == 0 {
		return true // Fail closed: a cut prefix with no checkable byte.
	}
	for _, rule := range privacyRules.magic {
		matched := true
		for _, test := range rule {
			for i, expected := range test.expected {
				at := test.offset + i
				if at >= len(decoded) {
					break
				}
				if decoded[at]&test.mask[i] != expected&test.mask[i] {
					matched = false
				}
			}
		}
		if matched {
			return true
		}
	}
	return false
}

// failClosedString reports a string the fail-closed walk must omit: any
// non-empty value under a media-capable path in an at-risk scope (no length,
// alphabet or text heuristic: headerless audio of any size and form is media
// once its MIME may be lost), or a possibly-cut value with a magic prefix.
// Existing replacement markers stay stable.
func (b *privacyBudget) failClosedString(value, path string, cut bool) bool {
	if !b.degraded || value == "" || stableReplacement(value) {
		return false
	}
	return b.atRisk(path) && failClosedMediaPath(path) || cut && magicPrefix(value)
}
func failClosedReplacement(value string) string {
	if base64Alphabet(value) {
		return omitted(value)
	}
	return privacyRules.Media.Other
}

// failClosedPartial rewrites unparseable (e.g. value-length truncated) JSON
// text in fail-closed mode. Unlike partialPrivacy it tracks the open
// object/array key stack, so a token keeps its media-capable ancestry. A
// string token under an at-risk media-capable path is replaced by its
// omission marker, as is a cut final token with a magic prefix; a bare
// number/bool literal there makes the whole value media (its samples cannot be
// rewritten safely without a parse). Linear in the already-charged text.
func failClosedPartial(s, path string, budget *privacyBudget) string {
	var out strings.Builder
	last := 0
	var stack []string
	key := ""
	current := func(leaf string) string {
		p := path
		for _, k := range stack {
			if k != "" {
				p = joinPath(p, k)
			}
		}
		if leaf != "" {
			p = joinPath(p, leaf)
		}
		return p
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '"':
			end := stringEnd(s, i)
			var token string
			complete := json.Unmarshal([]byte(s[i:end]), &token) == nil
			colon := end
			for colon < len(s) && jsonSpace(s[colon]) {
				colon++
			}
			if complete && colon < len(s) && s[colon] == ':' {
				key = token
				i = colon
				continue
			}
			if !complete {
				var ok bool
				if token, ok = privacyStringToken(s[i:end], budget.cutText); !ok {
					token = strings.TrimPrefix(s[i:end], `"`)
				}
			}
			if budget.failClosedString(token, current(key), !complete) {
				out.WriteString(s[last:i])
				out.WriteString(dump(failClosedReplacement(token)))
				last = end
			}
			i = end - 1
		case c == '{' || c == '[':
			stack = append(stack, key)
			key = ""
		case c == '}' || c == ']':
			if len(stack) > 0 {
				stack = stack[:len(stack)-1]
			}
			key = ""
		case c == ',':
			key = ""
		case c == '-' || c >= '0' && c <= '9' || c == 't' || c == 'f':
			if p := current(key); budget.atRisk(p) && failClosedMediaPath(p) {
				return privacyRules.Media.Other
			}
			for i+1 < len(s) && !strings.ContainsRune(",]} \t\r\n", rune(s[i+1])) {
				i++
			}
		}
	}
	if last == 0 {
		return s
	}
	out.WriteString(s[last:])
	return out.String()
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
		if (payload || budget.atRisk(path) && failClosedMediaPath(path)) && len(x) > 0 {
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
		// Preserve the cut before JSON/credential rewriting changes its length.
		// Descendant tokens inherit it, but unrelated sibling values do not.
		previousCut := budget.cutText
		budget.cutText = previousCut || budget.degraded && budget.risk.Truncated != nil && budget.risk.Truncated(x)
		defer func() { budget.cutText = previousCut }()
		if replacement, ok := mediaString(x, path, inherited, hints, framed); ok {
			return replacement
		}
		if budget.failClosedString(x, path, budget.cutText) {
			return failClosedReplacement(x)
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
		text := partialPrivacy(x, secrets, budget, depth+1)
		if budget.degraded && (strings.HasPrefix(trim, "{") || strings.HasPrefix(trim, "[")) {
			text = failClosedPartial(text, path, budget)
		}
		return budget.text(text, secrets, framed)
	case nil:
		return nil
	case bool, float64, int, int64, json.Number:
		_, payload := mediaContext(path, inherited, hints)
		if (payload || budget.atRisk(path) && failClosedMediaPath(path)) && !scalarMetric(x, path, inherited, hints) {
			return privacyRules.Media.Other
		}
		return x
	default:
		return fmt.Sprintf("%T", value)
	}
}
