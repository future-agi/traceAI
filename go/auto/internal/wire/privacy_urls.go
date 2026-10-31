package wire

import (
	"net/url"
	"sort"
	"strings"
)

// Data URIs have exact value boundaries in scalars and query values. Only prose
// uses token boundaries; never let a payload quote expose the rest of a scalar.
func dataURI(value string) (string, bool) {
	if len(value) < 5 || !strings.EqualFold(value[:5], "data:") {
		return "", false
	}
	metadata, payload, comma := strings.Cut(value[5:], ",")
	if !comma {
		return "", false
	}
	payload = percentDecode(payload, false)
	if strings.HasSuffix(asciiLower(metadata), ";base64") && (payload == "" || base64Alphabet(payload)) {
		return omitted(payload), true
	}
	return privacyRules.Media.DataURI.Other, true
}

// Like unquote/unquote_plus in the catalog reference, malformed percent escapes
// remain literal rather than discarding a whole credential-bearing parameter.
func percentDecode(s string, plus bool) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '%' && i+2 < len(s) {
			hi, lo := hexDigit(s[i+1]), hexDigit(s[i+2])
			if hi >= 0 && lo >= 0 {
				b.WriteByte(byte(hi*16 + lo))
				i += 2
				continue
			}
		}
		if s[i] == '+' && plus {
			b.WriteByte(' ')
		} else {
			b.WriteByte(s[i])
		}
	}
	return strings.ToValidUTF8(b.String(), "�")
}
func hexDigit(c byte) int {
	switch {
	case c >= '0' && c <= '9':
		return int(c - '0')
	case c >= 'a' && c <= 'f':
		return int(c - 'a' + 10)
	case c >= 'A' && c <= 'F':
		return int(c - 'A' + 10)
	}
	return -1
}
func textSpace(c byte) bool {
	return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\v' || c == '\f'
}
func schemeChar(c byte) bool {
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.ContainsRune("+.-_:", rune(c))
}
func prefixFold(value, prefix string) bool {
	return len(value) >= len(prefix) && strings.EqualFold(value[:len(prefix)], prefix)
}
func urlPrefix(s string) int {
	if prefixFold(s, "https://") {
		return 8
	}
	if prefixFold(s, "http://") {
		return 7
	}
	if len(s) > 0 && (s[0] == '/' || s[0] == '?') {
		return 1
	}
	return 0
}
func markerEnd(s string) int {
	if strings.HasPrefix(s, CredentialRedacted) {
		return len(CredentialRedacted)
	}
	if strings.HasPrefix(s, "<omitted:") {
		if len(s) > 64 {
			s = s[:64]
		} // A malformed marker cannot rescan an unbounded suffix.
		if i := strings.IndexByte(s, '>'); i >= 0 && omissionMarker.MatchString(s[:i+1]) {
			return i + 1
		}
	}
	return 0
}
func urlTokenEnd(s string) int {
	prefix := urlPrefix(s)
	if prefix == 0 {
		return 0
	}
	i := prefix
	for i < len(s) {
		if s[i] == '<' {
			if n := markerEnd(s[i:]); n > 0 {
				i += n
				continue
			}
		}
		if textSpace(s[i]) || strings.ContainsRune("\"<>\\", rune(s[i])) {
			break
		}
		i++
	}
	if i == prefix {
		return 0
	}
	return i
}
func dataTokenEnd(s string) int {
	i := 5
	for i < len(s) && !textSpace(s[i]) && !strings.ContainsRune("\"'<>\\", rune(s[i])) {
		i++
	}
	for i < len(s) && (s[i] == '\r' || s[i] == '\n') {
		start := i
		for i < len(s) && (s[i] == '\r' || s[i] == '\n') {
			i++
		}
		payload := i
		for i < len(s) && privacyRules.alphabet[s[i]] && s[i] != '\r' && s[i] != '\n' {
			i++
		}
		if i == payload {
			return start
		}
	}
	return i
}
func splitURL(original string) (base, question, query, hash, fragment string) {
	target := original
	if i := strings.IndexByte(target, '#'); i >= 0 {
		hash, fragment = "#", target[i+1:]
		target = target[:i]
	}
	base = target
	if i := strings.IndexByte(target, '?'); i >= 0 {
		base, question, query = target[:i], "?", target[i+1:]
	}
	return
}

type queryPair struct{ rawName, equals, rawValue, name, value string }

func queryPairs(raw string) []queryPair {
	parts := strings.Split(raw, "&")
	pairs := make([]queryPair, 0, len(parts))
	for _, part := range parts {
		name, value, equals := strings.Cut(part, "=")
		pair := queryPair{rawName: name, rawValue: value, name: percentDecode(name, true), value: percentDecode(value, true)}
		if equals {
			pair.equals = "="
		}
		pairs = append(pairs, pair)
	}
	return pairs
}
func isURLValue(value string) bool {
	if urlTokenEnd(value) == len(value) && len(value) > 0 {
		return true
	}
	if urlPrefix(value) == 0 {
		return false
	}
	base, question, query, hash, fragment := splitURL(value)
	if question == "" {
		return false
	}
	pairs := queryPairs(query)
	var b strings.Builder
	b.WriteString(base)
	b.WriteString(question)
	for i, pair := range pairs {
		if i > 0 {
			b.WriteByte('&')
		}
		b.WriteString(pair.rawName)
		b.WriteString(pair.equals)
		if _, ok := dataURI(pair.value); pair.equals != "" && ok {
			b.WriteString(privacyRules.Media.DataURI.Other)
		} else {
			b.WriteString(pair.rawValue)
		}
	}
	b.WriteString(hash)
	b.WriteString(fragment)
	candidate := b.String()
	return candidate != value && urlTokenEnd(candidate) == len(candidate)
}
func scrubURL(original string) string {
	base, question, query, hash, fragment := splitURL(original)
	if prefix := urlPrefix(base); prefix > 1 {
		// Only the authority may contain userinfo. Preserve path spelling.
		authority := base[prefix:]
		if end := strings.IndexByte(authority, '/'); end >= 0 {
			authority = authority[:end]
		}
		if at := strings.LastIndexByte(authority, '@'); at >= 0 && strings.Contains(authority[:at], ":") {
			base = base[:prefix] + CredentialRedacted + "@" + base[prefix+at+1:]
		}
	}
	pairs := queryPairs(query)
	hints := privacyHints{}
	for _, pair := range pairs {
		if pair.equals == "" {
			continue
		}
		if h := hint(pair.name, pair.value); h.media || h.audio {
			parent := hintParent(pair.name)
			hints[parent] = combineHint(hints[parent], h)
		}
	}
	var b strings.Builder
	b.WriteString(scanPrivacyText(base, false))
	b.WriteString(question)
	for i, pair := range pairs {
		if i > 0 {
			b.WriteByte('&')
		}
		b.WriteString(pair.rawName)
		b.WriteString(pair.equals)
		value := pair.rawValue
		if pair.equals != "" {
			if queryCredentialKey(pair.name) {
				value = CredentialRedacted
			} else if clean, ok := mediaString(pair.value, pair.name, mediaHint{}, hints, true); ok {
				value = clean
			} else if clean := scanDataText(pair.value, true); clean != pair.value {
				value = clean
			}
		}
		b.WriteString(value)
	}
	b.WriteString(hash)
	b.WriteString(scanPrivacyText(fragment, false))
	return b.String()
}
func scanDataText(s string, framed bool) string {
	if framed {
		if replacement, ok := dataURI(s); ok {
			return replacement
		}
	}
	return scanPrivacyText(s, false)
}

// A single forward scan. Failed data: prefixes consume their token as well, so
// long malformed media input cannot cause quadratic suffix rescanning.
func scanPrivacyText(s string, urls bool) string {
	var out strings.Builder
	last := 0
	for i := 0; i < len(s); {
		prefix := urlPrefix(s[i:])
		if urls && prefix > 0 && (prefix > 1 || i == 0 || textSpace(s[i-1])) {
			if end := urlTokenEnd(s[i:]); end > 0 {
				original := s[i : i+end]
				closing := ""
				if i > 0 && s[i-1] == '\'' && strings.HasSuffix(original, "'") {
					original = original[:len(original)-1]
					closing = "'"
				}
				out.WriteString(s[last:i])
				out.WriteString(scrubURL(original))
				out.WriteString(closing)
				i += end
				last = i
				continue
			}
		}
		if prefixFold(s[i:], "data:") && (i == 0 || !schemeChar(s[i-1])) {
			end := dataTokenEnd(s[i:])
			if replacement, ok := dataURI(s[i : i+end]); ok {
				out.WriteString(s[last:i])
				out.WriteString(replacement)
				last = i + end
			}
			i += end
			continue
		}
		i++
	}
	if last == 0 {
		return s
	}
	out.WriteString(s[last:])
	return out.String()
}
func privacyText(s string, secrets []string, framed bool) string {
	if framed && isURLValue(s) {
		s = scrubURL(s)
	} else {
		s = scanPrivacyText(s, true)
	}
	ordered := append([]string(nil), secrets...)
	sort.Slice(ordered, func(i, j int) bool {
		if len(ordered[i]) == len(ordered[j]) {
			return ordered[i] < ordered[j]
		}
		return len(ordered[i]) > len(ordered[j])
	})
	for _, secret := range ordered {
		if len(secret) >= 8 && secret != CredentialRedacted {
			parts := strings.Split(s, CredentialRedacted)
			for i := range parts {
				parts[i] = strings.ReplaceAll(parts[i], secret, CredentialRedacted)
			}
			s = strings.Join(parts, CredentialRedacted)
		}
	}
	return s
}

// querySecrets uses the same framing/name decoding as export-time URL handling.
func querySecrets(rawURL string) []string {
	// ParseRequestURI is not used: relative references are valid and names may
	// contain apostrophes/question marks. Only split the first ? before #.
	_, _, query, _, _ := splitURL(rawURL)
	var found []string
	for _, pair := range queryPairs(query) {
		if pair.equals != "" && queryCredentialKey(pair.name) {
			found = append(found, pair.value)
		}
	}
	// URL userinfo is also a credential crossing the transport.
	if parsed, err := url.Parse(rawURL); err == nil && parsed.User != nil {
		if password, ok := parsed.User.Password(); ok {
			found = append(found, password)
		}
	}
	return found
}
