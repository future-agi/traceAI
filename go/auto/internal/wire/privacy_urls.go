package wire

import (
	"net/url"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"
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

// dataBlocked is the reference's look-behind before data: ([A-Za-z0-9+.\-_:] under re.I, which also matches long s, dotted
// capital I, dotless i and the Kelvin sign): no data URI starts after such a character.
func dataBlocked(s string, i int) bool {
	if i == 0 {
		return false
	}
	if c := s[i-1]; c < utf8.RuneSelf {
		return schemeChar(c)
	}
	r, _ := utf8.DecodeLastRuneInString(s[:i])
	return r == '\u017f' || r == '\u0130' || r == '\u0131' || r == '\u212a'
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
		space, size := textSpaceAt(s, i)
		if space || strings.IndexByte("\"<>\\", s[i]) >= 0 {
			break
		}
		i += size
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

// rewrite is an output text built from kept and replaced pieces. It records by position (bytes) every span the catalog replaced:
// each placeholder, and each omission it writes (a data URI or media value), with its kind.
type rewrite struct {
	b     strings.Builder
	spans []pairSpan
}

func (w *rewrite) keep(s string) { w.b.WriteString(s) }
func (w *rewrite) put(s string, omission bool) {
	start := w.b.Len()
	w.b.WriteString(s)
	w.spans = append(w.spans, pairSpan{start, w.b.Len(), omission})
}

// region writes a part of a URL: each run of runs (sorted, disjoint; an empty run inserts the placeholder) becomes the
// placeholder, and each data URI in the original text its omission. Where a run and a data URI overlap, the run's placeholder is
// written and the data URI's omission covers the rest of it.
func (w *rewrite) region(text string, runs [][2]int) {
	type dataSpan struct {
		start, end  int
		replacement string
	}
	var data []dataSpan
	for _, m := range dataMatches(text) {
		if replacement, ok := dataURI(text[m[0]:m[1]]); ok {
			data = append(data, dataSpan{m[0], m[1], replacement})
		}
	}
	const none, run, uri = 0, 1, 2
	lastKind, lastIndex := none, -1
	position, length, r, d := 0, len(text), 0, 0
	for {
		for r < len(runs) && runs[r][1] <= position && !(runs[r][0] == runs[r][1] && runs[r][1] == position) {
			r++
		}
		if r < len(runs) && runs[r][0] == runs[r][1] && runs[r][0] == position {
			w.put(CredentialRedacted, false)
			lastKind, lastIndex = run, r
			r++
			continue
		}
		for d < len(data) && data[d].end <= position {
			d++
		}
		if position >= length {
			break
		}
		switch {
		case r < len(runs) && runs[r][0] <= position:
			if lastKind != run || lastIndex != r {
				w.put(CredentialRedacted, false)
				lastKind, lastIndex = run, r
			}
			position = runs[r][1]
		case d < len(data) && data[d].start <= position:
			end := data[d].end
			if r < len(runs) {
				end = min(end, runs[r][0])
			}
			if lastKind != uri || lastIndex != d {
				w.put(data[d].replacement, true)
				lastKind, lastIndex = uri, d
			}
			position = end
		default:
			end := length
			if r < len(runs) {
				end = min(end, runs[r][0])
			}
			if d < len(data) {
				end = min(end, data[d].start)
			}
			w.keep(text[position:end])
			position, lastKind, lastIndex = end, none, -1
		}
	}
}

// pathPairs is the URL path pair rule. Each path segment is read whole, every '=' and every %3D (%3d) in it: a pair's name runs
// back to the previous ';', ',', '&', '=' or %3D of the segment (the first separator also reads the name back to the segment
// start), percent-decoded once. A credential name's value is replaced to the next pair in the segment or the segment end. The
// scheme and authority of an absolute URL are not path segments.
func pathPairs(base string, out *rewrite) {
	path := 0
	if prefixFold(base, "http://") || prefixFold(base, "https://") {
		slashes := strings.Index(base, "//") + 2
		if path = strings.IndexByte(base[slashes:], '/'); path < 0 {
			path = len(base)
		} else {
			path += slashes
		}
	}
	var runs [][2]int
	for position := path; position < len(base); {
		end := strings.IndexByte(base[position+1:], '/')
		if end < 0 {
			end = len(base)
		} else {
			end += position + 1
		}
		first := position
		if base[position] == '/' {
			first = position + 1
		}
		k, name, decided := first, first, false
		for k < end {
			c := base[k]
			if pathPairSeparator(c) {
				k++
				name = k
				continue
			}
			size := 0
			if c == '=' {
				size = 1
			} else if encodedEqualsAt(base, k) {
				size = 3
			}
			if size == 0 {
				k++
				continue
			}
			credential := k > name && credentialName(percentDecode(base[name:k], false))
			if !decided && !credential && k > first {
				credential = credentialName(percentDecode(base[first:k], false))
			}
			decided = true
			if credential {
				valueEnd := pathValueEnd(base, k+size, end)
				runs = append(runs, [2]int{k + size, valueEnd})
				k, name = valueEnd, valueEnd
			} else {
				k += size
				name = k
			}
		}
		position = end
	}
	out.region(base, runs)
}

func pathPairSeparator(c byte) bool { return c == ';' || c == ',' || c == '&' }
func encodedEqualsAt(s string, k int) bool {
	return k+2 < len(s) && s[k] == '%' && s[k+1] == '3' && (s[k+2] == 'D' || s[k+2] == 'd')
}

// pathValueEnd is the end of a path pair's value in base[start:end] (one segment): the next ';', ',' or '&' that a non-empty
// name and then '=' or %3D follow, or the segment end.
func pathValueEnd(base string, start, end int) int {
	for k := start; k < end; {
		if !pathPairSeparator(base[k]) {
			k++
			continue
		}
		j := k + 1
		for j < end && !pathPairSeparator(base[j]) && base[j] != '=' && !encodedEqualsAt(base, j) {
			j++
		}
		if j < end && j > k+1 && !pathPairSeparator(base[j]) {
			return k
		}
		k = j
	}
	return end
}

// afterQuestion reports that a name after a '?' is a credential name: each name t[q+1:end] for a '?' q in marks is read. A name
// longer than the name window, or one that holds more than eight '?' marks, is not decided (true, fail closed).
func afterQuestion(t []rune, marks []int, end int) bool {
	if len(marks) == 0 {
		return false
	}
	if end-marks[0]-1 > pairNameCap || len(marks) > pairQuestionCap {
		return true
	}
	for _, q := range marks {
		if queryName(percentDecode(string(t[q+1:end]), true)) {
			return true
		}
	}
	return false
}

// questionName reports that a raw query name, or the part of it after any '?', is a credential name.
func questionName(rawName string) bool {
	if queryName(percentDecode(rawName, true)) {
		return true
	}
	if !strings.Contains(rawName, "?") {
		return false
	}
	t := []rune(rawName)
	var marks []int
	for k, c := range t {
		if c == '?' {
			if len(marks) > pairQuestionCap {
				break
			}
			marks = append(marks, k)
		}
	}
	return afterQuestion(t, marks, len(t))
}

// secondQuestionCut is where a value that holds '?name=' with a credential name is cut (after that pair's '='), as a byte
// offset, or -1. One forward pass: the '?' marks since the last '=' are kept (at most nine), and each '=' decides its names once.
func secondQuestionCut(rawValue string) int {
	if !strings.Contains(rawValue, "?") {
		return -1
	}
	t := make([]rune, 0, len(rawValue))
	offsets := make([]int, 0, len(rawValue))
	for at, c := range rawValue {
		t = append(t, c)
		offsets = append(offsets, at)
	}
	i := 0
	for t[i] != '?' {
		i++
	}
	var marks []int
	for ; i < len(t); i++ {
		switch t[i] {
		case '?':
			if len(marks) <= pairQuestionCap {
				marks = append(marks, i)
			}
		case '=':
			if len(marks) > 0 && afterQuestion(t, marks, i) {
				return offsets[i] + 1 // the byte after this '='
			}
			marks = marks[:0]
		}
	}
	return -1
}

// tailPairs is the catalog rule for the part of a URL after its '#': split at '&' like a query; a credential-named pair's value
// (the name read whole or after a '?') is replaced to the next '&'; in any other pair, a credential pair after a second '?'.
func tailPairs(segment string, out *rewrite) {
	var runs [][2]int
	offset := 0
	for _, piece := range strings.Split(segment, "&") {
		rawName, rawValue, equals := strings.Cut(piece, "=")
		value, end := offset+len(rawName)+1, offset+len(piece)
		if equals && questionName(rawName) {
			runs = append(runs, [2]int{value, end})
		} else if equals {
			if cut := secondQuestionCut(rawValue); cut >= 0 {
				runs = append(runs, [2]int{value + cut, end})
			}
		}
		offset = end + 1
	}
	out.region(segment, runs)
}

var userinfoPattern = regexp.MustCompile(`(?i)^(https?://)[^\s\v\x{1c}-\x{1f}\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}/@]+:[^\s\v\x{1c}-\x{1f}\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}/@]+@`)

// framedURL scrubs one already-framed URL. It returns the text and the spans (byte positions) the catalog itself replaced.
func framedURL(original string, cut bool) (string, []pairSpan) {
	out := &rewrite{}
	base, question, query, hash, fragment := splitURL(original)
	if m := userinfoPattern.FindStringSubmatchIndex(base); m != nil {
		out.keep(base[:m[3]])
		out.put(CredentialRedacted, false)
		out.keep("@")
		base = base[m[1]:]
	}
	pathPairs(base, out)
	out.keep(question)
	pairs := queryPairs(query)
	// Queries are flat objects: index every hint before scrubbing any value.
	hints := privacyHints{}
	for _, pair := range pairs {
		if pair.equals == "" {
			continue
		}
		pathBudget(pair.name)
		if h := hint(pair.name, pair.value); h.media || h.audio {
			parent := hintParent(pair.name)
			hints[parent] = combineHint(hints[parent], h)
		}
	}
	for i, pair := range pairs {
		if i > 0 {
			out.keep("&")
		}
		out.keep(pair.rawName + pair.equals)
		if pair.equals == "" {
			continue
		}
		if queryName(pair.name) || questionName(pair.rawName) {
			out.put(CredentialRedacted, false)
			continue
		}
		if clean, ok := mediaString(pair.value, pair.name, mediaHint{}, hints, true); ok {
			// The whole value is media: its omission covers a pair after a second '?' in it too.
			out.put(clean, true)
			continue
		}
		// A cut URL may have lost any later MIME/type sibling. Apply the same catalog-backed fail-closed rule as an attribute
		// object. Only its final query value can have lost magic bytes; ignore an unfinished percent escape when checking that
		// decoded prefix.
		prefix := pair.value
		cutValue := cut && hash == "" && i == len(pairs)-1
		if cutValue {
			if at := strings.LastIndexByte(pair.rawValue, '%'); at >= 0 && len(pair.rawValue)-at < 3 {
				prefix = percentDecode(pair.rawValue[:at], true)
			}
		}
		risk := privacyBudget{degraded: cut}
		if risk.failClosedString(pair.value, pair.name, false) || cutValue && magicPrefix(prefix) {
			out.put(failClosedReplacement(pair.value), true)
			continue
		}
		if at := secondQuestionCut(pair.rawValue); at >= 0 {
			// A credential-named pair after a second '?' goes to the value's end.
			out.keep(pair.rawValue[:at])
			out.put(CredentialRedacted, false)
		} else if scanDataText(pair.value, true) != pair.value {
			// The decoded value is written with each data URI replaced; only those are spans.
			out.region(pair.value, nil)
		} else {
			out.keep(pair.rawValue)
		}
	}
	out.keep(hash)
	if hash != "" {
		tailPairs(fragment, out)
	}
	return out.b.String(), out.spans
}

func scrubURL(original string, cut bool) string {
	text, _ := framedURL(original, cut)
	return text
}

func scanDataText(s string, framed bool) string {
	if framed {
		if replacement, ok := dataURI(s); ok {
			return replacement
		}
	}
	return scanPrivacyText(s, false, false)
}

// textSpaceAt reports Unicode whitespace (the reference's \s) at s[i], and its width.
func textSpaceAt(s string, i int) (bool, int) {
	if c := s[i]; c < utf8.RuneSelf {
		return pySpace(rune(c)), 1
	}
	c, size := utf8.DecodeRuneInString(s[i:])
	return pySpace(c), size
}

// dataMatchEnd is the end of the data URI token that starts at i (the reference's DATA pattern, before the I4 cut), or -1.
func dataMatchEnd(s string, i int) int {
	if !prefixFold(s[i:], "data:") || dataBlocked(s, i) {
		return -1
	}
	k := i + 5
	for k < len(s) {
		if space, size := textSpaceAt(s, k); space || strings.IndexByte("\"'<>\\", s[k]) >= 0 {
			break
		} else {
			k += size
		}
	}
	for k < len(s) && (s[k] == '\r' || s[k] == '\n') {
		run := k
		for k < len(s) && (s[k] == '\r' || s[k] == '\n') {
			k++
		}
		body := k
		for k < len(s) && dataLineChar(s[k]) {
			k++
		}
		if k == body {
			return run
		}
	}
	return k
}

func dataLineChar(c byte) bool {
	return c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '+' || c == '/' || c == '_' || c == '=' || c == '-'
}

// encodedSpaceAheadText is encodedSpaceAhead over a string (every encoded form is ASCII).
func encodedSpaceAheadText(s string, k int) int {
	if k >= len(s) || s[k] != '%' && s[k] != '\\' {
		return 0
	}
	end := min(len(s), k+6)
	return encodedSpaceAhead([]rune(s[k:end]), 0)
}

// dataURIEnd is the end of the data URI matched at s[start:end] (I4): a base64 continuation line belongs to it only if its '='
// characters are trailing base64 padding, and a line followed by a separator is a pair name. The match is cut before the first
// line that does not belong.
func dataURIEnd(s string, start, end int) int {
	head := start + 5
	for head < end && s[head] != '\r' && s[head] != '\n' {
		head++
	}
	metadata, payload, comma := strings.Cut(s[start+5:head], ",")
	base64 := comma && strings.HasSuffix(strings.ToLower(metadata), ";base64")
	decoded := percentDecode(payload, false)
	count, padded := 0, strings.Contains(decoded, "=")
	for i := 0; i < len(decoded); i++ {
		if dataLineChar(decoded[i]) && decoded[i] != '=' {
			count++
		}
	}
	position := head
	for position < end {
		k := position
		for k < end && (s[k] == '\r' || s[k] == '\n') {
			k++
		}
		body := k
		for k < end && dataLineChar(s[k]) {
			k++
		}
		if body == position || k == body {
			break
		}
		after, gap := k, k
		for gap < len(s) {
			if width := encodedSpaceAheadText(s, gap); width > 0 {
				gap += width
				continue
			}
			c, size := utf8.DecodeRuneInString(s[gap:])
			if !dashesSpace(c) {
				break
			}
			gap += size
		}
		if gap < len(s) && (s[gap] == '=' || s[gap] == ':' || encodedEqualsAt(s, gap)) {
			break // a name, a gap and a separator: a pair, never base64
		}
		line := s[body:after]
		trimmed := strings.TrimRight(line, "=")
		pad := len(line) - len(trimmed)
		if strings.HasPrefix(trimmed, "--") || strings.Contains(trimmed, "=") || pad > 0 && !base64Padding(trimmed, pad, base64, padded, count) {
			break
		}
		count, padded, position = count+len(trimmed), padded || pad > 0, after
	}
	return position
}

// base64Padding reports that pad '=' characters after body are trailing base64 padding.
func base64Padding(body string, pad int, base64, padded bool, count int) bool {
	if !base64 || padded || pad > 2 || body == "" || strings.HasPrefix(body, "-") || (count+len(body)+pad)%4 != 0 {
		return false
	}
	index := strings.IndexByte("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/", body[len(body)-1])
	switch body[len(body)-1] {
	case '-':
		index = 62
	case '_':
		index = 63
	}
	if pad == 1 {
		return index%4 == 0
	}
	return index%16 == 0
}

// dataMatches are the data URIs in s, left to right, each cut by dataURIEnd.
func dataMatches(s string) [][2]int {
	var found [][2]int
	for i := 0; i < len(s); {
		end := dataMatchEnd(s, i)
		if end < 0 {
			i++
			continue
		}
		end = dataURIEnd(s, i, end)
		found = append(found, [2]int{i, end})
		if end > i {
			i = end
		} else {
			i++
		}
	}
	return found
}

// A single forward scan over URL and data URI tokens. Failed data: prefixes consume their token as well, so long malformed
// media input cannot cause quadratic suffix rescanning.
func scanPrivacyText(s string, urls, cut bool) string {
	var out strings.Builder
	last := 0
	for i := 0; i < len(s); {
		prefix := urlPrefix(s[i:])
		if urls && prefix > 0 && (prefix > 1 || i == 0 || precededBySpace(s, i)) {
			if end := urlTokenEnd(s[i:]); end > 0 {
				original := s[i : i+end]
				closing := ""
				if i > 0 && s[i-1] == '\'' && strings.HasSuffix(original, "'") {
					original = original[:len(original)-1]
					closing = "'"
				}
				out.WriteString(s[last:i])
				out.WriteString(scrubURL(original, cut))
				out.WriteString(closing)
				i += end
				last = i
				continue
			}
		}
		if end := dataMatchEnd(s, i); end >= 0 {
			end = dataURIEnd(s, i, end)
			if replacement, ok := dataURI(s[i:end]); ok {
				out.WriteString(s[last:i])
				out.WriteString(replacement)
				last = end
			}
			if end > i {
				i = end
			} else {
				i++
			}
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

func precededBySpace(s string, i int) bool {
	c, _ := utf8.DecodeLastRuneInString(s[:i])
	return pySpace(c)
}

func orderedSecrets(secrets []string) []string {
	var ordered []string
	seen := map[string]bool{}
	for _, secret := range secrets {
		if utf8.RuneCountInString(secret) >= 8 && secret != CredentialRedacted && !seen[secret] {
			seen[secret] = true
			ordered = append(ordered, secret)
		}
	}
	sort.Slice(ordered, func(i, j int) bool {
		if a, b := utf8.RuneCountInString(ordered[i]), utf8.RuneCountInString(ordered[j]); a != b {
			return a > b
		}
		return ordered[i] < ordered[j]
	})
	return ordered
}

// replaceRuns replaces each run (sorted, disjoint byte positions) with the placeholder. A span a run overlaps is dropped; the
// others move.
func replaceRuns(value string, runs [][2]int, spans []pairSpan) (string, []pairSpan) {
	if len(runs) == 0 {
		return value, spans
	}
	var out strings.Builder
	var moved []pairSpan
	copied, shift, k := 0, 0, 0
	for _, r := range runs {
		for k < len(spans) && spans[k].end <= r[0] {
			moved = append(moved, pairSpan{spans[k].start + shift, spans[k].end + shift, spans[k].omission})
			k++
		}
		for k < len(spans) && spans[k].start < r[1] {
			k++
		}
		out.WriteString(value[copied:r[0]])
		out.WriteString(CredentialRedacted)
		copied = r[1]
		shift += len(CredentialRedacted) - (r[1] - r[0])
	}
	for _, span := range spans[k:] {
		moved = append(moved, pairSpan{span.start + shift, span.end + shift, span.omission})
	}
	out.WriteString(value[copied:])
	return out.String(), moved
}

// maskOutsideSpans applies a known-secret masker to the text between the recorded spans, moving the spans.
func maskOutsideSpans(value string, spans []pairSpan, mask func(string) string) (string, []pairSpan) {
	var out strings.Builder
	moved := make([]pairSpan, 0, len(spans))
	copied := 0
	for _, span := range spans {
		out.WriteString(mask(value[copied:span.start]))
		start := out.Len()
		out.WriteString(value[span.start:span.end])
		moved = append(moved, pairSpan{start, out.Len(), span.omission})
		copied = span.end
	}
	out.WriteString(mask(value[copied:]))
	return out.String(), moved
}

// runeSpans converts byte spans (sorted) of value to rune positions.
func runeSpans(value string, spans []pairSpan) []pairSpan {
	out := make([]pairSpan, len(spans))
	at, runes := 0, 0
	position := func(b int) int {
		if b < at {
			at, runes = 0, 0
		}
		runes += utf8.RuneCountInString(value[at:b])
		at = b
		return runes
	}
	for i, span := range spans {
		out[i] = pairSpan{position(span.start), position(span.end), span.omission}
	}
	return out
}

// privacyText is the export text rule of one string: the URL catalog and data URIs, known secrets (the listed values, then the
// registered masker), the catalog's value shapes, then the credential pair rule. The pair rule does not read the re-dumped JSON of
// a structured string (each of its leaf strings went through the rule on its own). For a whole URL value at a framed site, the
// pair rule reads the URL text and skips only the spans the catalog itself replaced, recorded by position.
func privacyText(s string, secrets []string, framed, structured, cut bool, mask func(string) string) string {
	ordered := orderedSecrets(secrets)
	if !(framed && isURLValue(s)) {
		s = scanPrivacyText(s, true, cut)
		for _, secret := range ordered {
			parts := strings.Split(s, CredentialRedacted)
			for i := range parts {
				parts[i] = strings.ReplaceAll(parts[i], secret, CredentialRedacted)
			}
			s = strings.Join(parts, CredentialRedacted)
		}
		if mask != nil {
			s = mask(s)
		}
		s = redactValueShapes(s)
		if structured {
			return s
		}
		return credentialPairs(s, nil)
	}
	value, spans := framedURL(s, cut)
	for _, secret := range ordered {
		var runs [][2]int
		offset := 0
		for _, part := range strings.Split(value, CredentialRedacted) {
			for found := 0; ; {
				at := strings.Index(part[found:], secret)
				if at < 0 {
					break
				}
				runs = append(runs, [2]int{offset + found + at, offset + found + at + len(secret)})
				found += at + len(secret)
			}
			offset += len(part) + len(CredentialRedacted)
		}
		value, spans = replaceRuns(value, runs, spans)
	}
	if mask != nil {
		value, spans = maskOutsideSpans(value, spans, mask)
	}
	for k := range privacyRules.shapes {
		value, spans = replaceRuns(value, shapeRuns(k, value), spans)
	}
	return credentialPairs(value, runeSpans(value, spans))
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
