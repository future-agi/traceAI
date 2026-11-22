package wire

// The credential pair rule of the shared privacy catalog (privacy.json v4). Every exported string passes known-secret replacement,
// then the catalog's value shapes, then this rule: the value of every credential-named name=value, name: value, "name": value or
// --name value runs to the true end of its value and becomes the placeholder. Positions and the bounded windows (8 spaces, 128
// name characters, 130 for a quoted name, 512 and 1024 for user information) count code points, as the shared reference does, so
// the rule reads its text as runes.

import (
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"
)

const (
	pairNameCap     = 128 // a bounded name is read through its last pairNameCap characters
	pairQuestionCap = 8   // a name after a '?' that holds more '?' marks than this is undecided
	pairGapCap      = 8   // the longest readable gap between a name and its separator (plain spaces)
	escapedQuote    = `\"`
	entityQuote     = "&quot;"
)

// pathBudgetExceeded is raised (as a panic) for a name of more than limits.max_depth components: a path, a map key, a hint name
// or a name in text. The walk that exports the value recovers it and exports the whole value as the placeholder (fail closed).
type pathBudgetExceeded struct{}

// pathBudget fails closed on a name of more than limits.max_depth components (each non-empty run between '.', '[' and ']').
func pathBudget(name string) {
	separators := 0
	for i := 0; i < len(name); i++ {
		if name[i] == '.' || name[i] == '[' || name[i] == ']' {
			separators++
		}
	}
	if separators >= privacyRules.Limits.Depth && len(pathParts(name)) > privacyRules.Limits.Depth {
		panic(pathBudgetExceeded{})
	}
}

// pySpace is the reference's whitespace (Python str.isspace): never unicode.IsSpace, which differs on U+001C-U+001F.
func pySpace(c rune) bool {
	switch {
	case c >= '\t' && c <= '\r', c >= 0x1c && c <= 0x20, c == 0x85, c == 0xa0, c == 0x1680, c >= 0x2000 && c <= 0x200a,
		c == 0x2028, c == 0x2029, c == 0x202f, c == 0x205f, c == 0x3000:
		return true
	}
	return false
}

// javaSpace is Java's Character.isWhitespace: U+0009-U+000D, U+001C-U+001F and the Unicode space, line and paragraph separators
// except the no-break spaces.
func javaSpace(c rune) bool {
	if c < utf8.RuneSelf {
		return c == ' ' || c >= '\t' && c <= '\r' || c >= 0x1c && c <= 0x1f
	}
	switch {
	case c >= '\t' && c <= '\r', c >= 0x1c && c <= 0x1f:
		return true
	case c == 0xa0 || c == 0x2007 || c == 0x202f:
		return false
	}
	return unicode.In(c, unicode.Zs, unicode.Zl, unicode.Zp)
}

func pairNameChar(c rune) bool {
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == '-' || c == '[' || c == ']'
}
func pairNameStart(c rune) bool { return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c == '_' }
func hexRune(c rune) bool       { return c < utf8.RuneSelf && hexDigit(byte(c)) >= 0 }
func lineBreak(c rune) bool {
	return c == '\n' || c == '\r' || c == 0x85 || c == 0x2028 || c == 0x2029 || c == '\f' || c == '\v'
}

// dashesSpace is the gap after a --name: any whitespace that is not a line break (a line break ends the --name on its line).
func dashesSpace(c rune) bool { return pySpace(c) && !lineBreak(c) }

var pairListHeaders = map[string]bool{"cookie": true, "setcookie": true, "authorization": true, "proxyauthorization": true}

// runesAt reports whether s starts at t[k].
func runesAt(t []rune, k int, s string) bool {
	if k < 0 {
		return false
	}
	for _, c := range s {
		if k >= len(t) || t[k] != c {
			return false
		}
		k++
	}
	return true
}

// runesFind is the first k in [lo, hi-len(s)] where s starts, or -1.
func runesFind(t []rune, s string, lo, hi int) int {
	n := utf8.RuneCountInString(s)
	if hi > len(t) {
		hi = len(t)
	}
	for k := max(lo, 0); k+n <= hi; k++ {
		if runesAt(t, k, s) {
			return k
		}
	}
	return -1
}

// runesFindLast is the last k in [lo, hi-len(s)] where s starts, or -1.
func runesFindLast(t []rune, s string, lo, hi int) int {
	n := utf8.RuneCountInString(s)
	if hi > len(t) {
		hi = len(t)
	}
	for k := hi - n; k >= max(lo, 0); k-- {
		if runesAt(t, k, s) {
			return k
		}
	}
	return -1
}

// runeIndex is the first k in [lo, hi) with t[k] == c, or -1.
func runeIndex(t []rune, c rune, lo, hi int) int {
	for k := max(lo, 0); k < min(hi, len(t)); k++ {
		if t[k] == c {
			return k
		}
	}
	return -1
}

// runeLastIndex is the last k in [lo, hi) with t[k] == c, or -1.
func runeLastIndex(t []rune, c rune, lo, hi int) int {
	for k := min(hi, len(t)) - 1; k >= max(lo, 0); k-- {
		if t[k] == c {
			return k
		}
	}
	return -1
}

// encodedEquals reports %3D or %3d at t[k].
func encodedEquals(t []rune, k int) bool {
	return k+2 < len(t) && t[k] == '%' && t[k+1] == '3' && (t[k+2] == 'D' || t[k+2] == 'd')
}

// encodedEqualsDigits reports the two characters "3D" or "3d" at t[k].
func encodedEqualsDigits(t []rune, k int) bool {
	return k >= 0 && k+1 < len(t) && t[k] == '3' && (t[k+1] == 'D' || t[k+1] == 'd')
}

func upperASCII(c rune) rune {
	if c >= 'a' && c <= 'z' {
		return c - 'a' + 'A'
	}
	return c
}

// encodedSpaceDigits reports the hex digits (any case) of %20, %09, %0A, %0B, %0C or %0D at t[k].
func encodedSpaceDigits(t []rune, k int) bool {
	if k < 0 || k+1 >= len(t) {
		return false
	}
	a, b := t[k], upperASCII(t[k+1])
	return a == '2' && b == '0' || a == '0' && (b == '9' || b == 'A' || b == 'B' || b == 'C' || b == 'D')
}

// encodedNBSP reports %C2%A0 (any hex case) at t[k].
func encodedNBSP(t []rune, k int) bool {
	if k < 0 || k+6 > len(t) {
		return false
	}
	for j, c := range "%C2%A0" {
		if upperASCII(t[k+j]) != c {
			return false
		}
	}
	return true
}

func jsonSpaceEscape(c rune) bool { return c == 't' || c == 'n' || c == 'r' || c == 'f' || c == 'b' }

// hexValue reads four hex digits at t[k], or -1.
func hexValue(t []rune, k int) rune {
	if k < 0 || k+4 > len(t) {
		return -1
	}
	var v rune
	for _, c := range t[k : k+4] {
		if !hexRune(c) {
			return -1
		}
		v = v*16 + rune(hexDigit(byte(c)))
	}
	return v
}

// nextPair reports whether another pair starts at start: [ \t]{0,8} (spaced only), -{0,2}, a name of 1 to 128 characters that
// starts with a letter or underscore, [ ]{0,8} (spaced only), then ':' or an '=' that is not padding or an empty value.
func nextPair(t []rune, start int, spaced bool) bool {
	length, i := len(t), start
	if spaced {
		for i < length && (t[i] == ' ' || t[i] == '\t') && i-start <= 8 {
			i++
		}
		if i-start > 8 {
			return false
		}
	}
	dashes := i
	for i < length && t[i] == '-' && i-dashes <= 2 {
		i++
	}
	if i-dashes > 2 || i >= length || !pairNameStart(t[i]) {
		return false
	}
	name := i
	i++
	for i < length && pairNameChar(t[i]) && i-name < 128 {
		i++
	}
	if i < length && pairNameChar(t[i]) {
		return false
	}
	if spaced {
		gap := i
		for i < length && t[i] == ' ' && i-gap <= 8 {
			i++
		}
		if i-gap > 8 {
			return false
		}
	}
	if i >= length {
		return false
	}
	if t[i] == ':' {
		return true
	}
	if t[i] != '=' {
		return false
	}
	rest := i + 1
	if rest >= length || strings.ContainsRune("= \t\n\v\f\r,;)]}\"'", t[rest]) {
		return false
	}
	if length-rest <= 2 {
		switch string(t[rest:]) {
		case "\r\n", "\n", "\r", "\u0085", "\u2028", "\u2029":
			return false
		}
	}
	return true
}

// pairValueEnd is the true end of a name=value value: a pair boundary with no bracket open, or the end of the text. A value that
// starts with a private key header runs to the key's end line; a quoted value is read to its closing quote and then on.
func pairValueEnd(t []rune, start int) int {
	length := len(t)
	if start >= length {
		return length
	}
	if runesAt(t, start, "-----BEGIN") {
		end := runesFind(t, "-----END", start, length)
		if end < 0 {
			return length
		}
		closing := runesFind(t, "-----", end+8, length)
		if closing < 0 {
			return length
		}
		return closing + 5
	}
	first, i := t[start], start
	if first == '"' || first == '\'' {
		i = start + 1
		for i < length && t[i] != first {
			if t[i] == '\\' {
				i += 2
			} else {
				i++
			}
		}
		if i >= length {
			return length
		}
		i++
	}
	depth := 0
	for ; i < length; i++ {
		switch c := t[i]; {
		case c == '(' || c == '[' || c == '{':
			depth++
		case c == ')' || c == ']' || c == '}':
			depth = max(depth-1, 0)
		case depth == 0:
			if (c == ',' || c == ';' || c == '\n' || c == '\r') && nextPair(t, i+1, true) {
				return i
			}
			if c == ' ' && nextPair(t, i+1, false) {
				return i
			}
		}
	}
	return length
}

// encodedSpaceWidth is the width of the encoded whitespace that ends at end, read back no further than floor, or 0: a percent
// escape (%20, %09, %0A-%0D or %C2%A0, any hex case) or a JSON string escape (\t \n \r \f \b, or a \u escape of whitespace). A
// backslash that an earlier backslash escapes starts no escape.
func encodedSpaceWidth(t []rune, end, floor int) int {
	if end-3 >= floor && t[end-3] == '%' && encodedSpaceDigits(t, end-2) {
		return 3
	}
	if end-6 >= floor && encodedNBSP(t, end-6) {
		return 6
	}
	width := 0
	if end-2 >= floor && t[end-2] == '\\' && jsonSpaceEscape(t[end-1]) {
		width = 2
	} else if end-6 >= floor && t[end-6] == '\\' && t[end-5] == 'u' {
		if v := hexValue(t, end-4); v >= 0 && pySpace(v) {
			width = 6
		}
	}
	if width == 0 {
		return 0
	}
	slashes := 1
	for end-width-slashes >= floor && t[end-width-slashes] == '\\' {
		slashes++
	}
	if slashes%2 == 1 {
		return width
	}
	return 0
}

// gapStart is the start of the gap (whitespace and encoded whitespace, in any order) that ends at end, read back to floor.
func gapStart(t []rune, end, floor int) int {
	for end > floor {
		if pySpace(t[end-1]) {
			end--
			continue
		}
		width := encodedSpaceWidth(t, end, floor)
		if width == 0 {
			break
		}
		end -= width
	}
	return end
}

// escapeBefore reports a percent escape (a '%' and two hex digits) that ends at end.
func escapeBefore(t []rune, end, floor int) bool {
	return end-3 >= floor && t[end-3] == '%' && hexRune(t[end-2]) && hexRune(t[end-1])
}

var percentEscapePattern = regexp.MustCompile(`%[0-9A-Fa-f]{2}`)

// escapedName is the name before a separator that ends at a percent escape, read back over name characters and %XX escapes (an
// encoded separator ends it) and percent-decoded once. ok is false when the name does not end at an escape. The pair is
// undecided when a %XX remains after the one decode, when the read found no letter, digit or underscore, or when the name runs on
// past its window.
func escapedName(t []rune, nameStart, nameEnd, floor int) (decoded string, undecided, ok bool) {
	if !(escapeBefore(t, nameEnd, floor) && !encodedEqualsDigits(t, nameEnd-2) ||
		nameStart > floor && escapeBefore(t, nameStart, floor) && t[nameStart-2] == '2' && t[nameStart-1] == '5') {
		return "", false, false
	}
	start := nameEnd
	for start > floor && nameEnd-start < pairNameCap {
		if escapeBefore(t, start, floor) {
			if encodedEqualsDigits(t, start-2) {
				break
			}
			start -= 3
		} else if pairNameChar(t[start-1]) {
			start--
		} else {
			break
		}
	}
	overflow := nameEnd-start >= pairNameCap && start > floor && (pairNameChar(t[start-1]) || escapeBefore(t, start, floor))
	decoded = percentDecode(string(t[start:nameEnd]), false)
	named := false
	for _, c := range decoded {
		named = named || c == '_' || unicode.IsLetter(c) || unicode.IsNumber(c)
	}
	return decoded, overflow || percentEscapePattern.MatchString(decoded) || !named, true
}

// encodedControl reports that the encoded whitespace of width at start stands for a control character: %09-%0D (any hex case),
// the JSON escapes \t \n \r \f \b, and a \u escape of a control. %20, %C2%A0 and a \u escape of a space are no controls.
func encodedControl(t []rune, start, width int) bool {
	if t[start] == '%' {
		return width == 3 && !(t[start+1] == '2' && t[start+2] == '0')
	}
	if width == 2 {
		return true
	}
	return unicode.Is(unicode.Cc, hexValue(t, start+2))
}

// separatorAt is the width of the pair separator at k ('=', a ':' that does not open "://", or %3D / %3d), or 0.
func separatorAt(t []rune, k int) int {
	c := t[k]
	if c == '=' || c == ':' && !runesAt(t, k+1, "//") {
		return 1
	}
	if c == '%' && encodedEquals(t, k) {
		return 3
	}
	return 0
}

// separatorValueEnd is the true end of the value of the pair whose separator (of size) is at k: up to 8 spaces after the
// separator, then pairValueEnd, or the line end after a list header name.
func separatorValueEnd(t []rune, k, size int) int {
	value := k + size
	for value < len(t) && value-k-size < 8 && t[value] == ' ' {
		value++
	}
	name := k
	for name > 0 && k-name < pairNameCap && pairNameChar(t[name-1]) {
		name--
	}
	if pairListHeaders[privacyRules.normalize(string(t[name:k]))] {
		return lineEnd(t, value)
	}
	return pairValueEnd(t, value)
}

func lineEnd(t []rune, k int) int {
	for k < len(t) && t[k] != '\n' && t[k] != '\r' {
		k++
	}
	return k
}

// consumedEnd: no rule leaves an '=', ':' or %3D unread at the end of the text it consumed. While t[start:stop] ends at a
// separator that is a credential or undecided pair (decide gives that pair's value end, or -1), the replacement runs on to that
// pair's value end.
func consumedEnd(t []rune, start, stop int, decide func(k, size, floor int, readEnd bool) int) int {
	for stop > start {
		size := 0
		if (t[stop-1] == '=' || t[stop-1] == ':') && separatorAt(t, stop-1) != 0 {
			size = 1
		} else if stop-3 >= start && separatorAt(t, stop-3) == 3 {
			size = 3
		} else {
			return stop
		}
		end := decide(stop-size, size, start, true)
		if end <= stop {
			return stop
		}
		stop = end
	}
	return stop
}

// encodedSpaceAhead is the width of the encoded whitespace that starts at start, or 0.
func encodedSpaceAhead(t []rune, start int) int {
	if start >= len(t) || start < 0 {
		return 0
	}
	if t[start] == '%' {
		if encodedSpaceDigits(t, start+1) {
			return 3
		}
		if encodedNBSP(t, start) {
			return 6
		}
		return 0
	}
	if t[start] == '\\' && start+1 < len(t) {
		if jsonSpaceEscape(t[start+1]) {
			return 2
		}
		if t[start+1] == 'u' {
			if v := hexValue(t, start+2); v >= 0 && pySpace(v) {
				return 6
			}
		}
	}
	return 0
}

// gapAhead is the end of the horizontal and encoded whitespace that starts at k.
func gapAhead(t []rune, k int) int {
	for k < len(t) {
		if dashesSpace(t[k]) {
			k++
		} else if width := encodedSpaceAhead(t, k); width > 0 {
			k += width
		} else {
			break
		}
	}
	return k
}

// dashesSeparator reports that the word after a --name and its gap starts with a separator ('=', ':' or %3D).
func dashesSeparator(t []rune, start int) bool {
	return start < len(t) && (t[start] == '=' || t[start] == ':' || encodedEquals(t, start))
}

// flagDashes is 2 for "--", 1 for a single dash followed by a name start, else 0.
func flagDashes(t []rune, i int) int {
	if runesAt(t, i, "--") {
		return 2
	}
	if runesAt(t, i, "-") && i+1 < len(t) && pairNameStart(t[i+1]) {
		return 1
	}
	return 0
}

// readableGap: 0 to 8 plain spaces. Any other whitespace, encoded whitespace included, leaves the name undecided.
func readableGap(t []rune, start, end int) bool {
	if end-start > pairGapCap {
		return false
	}
	for k := start; k < end; k++ {
		if t[k] != ' ' {
			return false
		}
	}
	return true
}

// quoteBefore is the quote spelling that ends at end: &quot;, an escaped quote, a quote or an apostrophe; or "".
func quoteBefore(t []rune, end int) string {
	if end >= 6 && runesAt(t, end-6, entityQuote) {
		return entityQuote
	}
	if end < 1 || t[end-1] != '"' && t[end-1] != '\'' {
		return ""
	}
	if t[end-1] == '"' {
		slashes := 0
		for end-2-slashes >= 0 && t[end-2-slashes] == '\\' && slashes < pairNameCap {
			slashes++
		}
		if slashes%2 == 1 {
			return escapedQuote
		}
	}
	return string(t[end-1])
}

// quotedName is a quoted name that ends, after a whitespace gap, before a separator.
type quotedName struct {
	opening, close int
	quote          string
	overflow       bool
}

// jsonName reads the quoted name before the separator at colon. The opening quote is looked for only within the 130 characters
// before the closing one; when that window ends inside the text without one, the name is not read (overflow, opening -1). A gap
// that is not readable leaves the name undecided (overflow).
func jsonName(t []rune, colon int) (quotedName, bool) {
	end := gapStart(t, colon, 0)
	quote := quoteBefore(t, end)
	if quote == "" || len(quote) == 1 && end < 3 {
		return quotedName{}, false
	}
	closing := end - len(quote)
	floor := max(0, closing-130)
	if opening := runesFindLast(t, quote, floor, closing); opening >= 0 {
		return quotedName{opening, closing, quote, !readableGap(t, end, colon)}, true
	}
	if floor > 0 {
		return quotedName{-1, closing, quote, true}, true
	}
	return quotedName{}, false
}

// jsonStringEnd is the index after a quoted string that starts at start (a backslash escapes the next character).
func jsonStringEnd(t []rune, start int) int {
	quote := t[start]
	for i := start + 1; i < len(t); i++ {
		if t[i] == '\\' {
			i++
			continue
		}
		if t[i] == quote {
			return i + 1
		}
	}
	return len(t)
}

func jsonMemberBoundary(t []rune, end int) bool {
	for end < len(t) && javaSpace(t[end]) {
		end++
	}
	return end >= len(t) || t[end] == ',' || t[end] == '}' || t[end] == ']'
}

// escapedStringEnd is the index after an escaped JSON string inside a JSON string (\"...\") that starts at start.
func escapedStringEnd(t []rune, start int) int {
	length, i, innerEscape := len(t), start+2, false
	for i < length {
		if t[i] == '\\' && i+1 < length {
			outer := t[i+1]
			i += 2
			if innerEscape {
				innerEscape = false
			} else if outer == '\\' {
				innerEscape = true
			} else if outer == '"' {
				return i
			}
		} else {
			i++
			innerEscape = false
		}
	}
	return length
}

// entityStringEnd is the index after a string quoted with &quot; that starts at start.
func entityStringEnd(t []rune, start int) int {
	length, i := len(t), start+6
	for i < length {
		if t[i] == '\\' {
			if runesAt(t, i+1, entityQuote) {
				i += 7
			} else {
				i += 2
			}
		} else if runesAt(t, i, entityQuote) {
			return i + 6
		} else {
			i++
		}
	}
	return length
}

var jsonNumberPattern = regexp.MustCompile(`^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$`)

// jsonValueEnd is the end of the value of a quoted credential name: (a) a quoted string ends at its closing quote, in the
// spelling of the name's quote; (b) a number, true, false or null ends before ',', '}', ']' or the end of the text; (c) anything
// else is prose and runs to its true end (the line end for a list header).
func jsonValueEnd(t []rune, start int, listHeader bool, quote string) int {
	length := len(t)
	if start >= length {
		return start
	}
	if quote == entityQuote && runesAt(t, start, entityQuote) {
		return entityStringEnd(t, start)
	}
	if quote == escapedQuote && runesAt(t, start, escapedQuote) {
		return escapedStringEnd(t, start)
	}
	if t[start] == '"' || t[start] == '\'' {
		return jsonStringEnd(t, start)
	}
	end := start
	for end < length && strings.ContainsRune("0123456789+-.eEtrufalsn", t[end]) {
		end++
	}
	if (end >= length || strings.ContainsRune(",}] \t\r\n", t[end])) && jsonMemberBoundary(t, end) {
		scalar := string(t[start:end])
		if scalar == "true" || scalar == "false" || scalar == "null" || jsonNumberPattern.MatchString(scalar) {
			return end
		}
	}
	if listHeader {
		return lineEnd(t, start)
	}
	return pairValueEnd(t, start)
}

// pairName is a credential name for the pair rule, with the command-line and bracket spellings undone first: --password,
// -Ddb.password and user[password].
func pairName(name string) bool {
	if name == "" {
		return false
	}
	stripped := strings.TrimLeft(name, "-")
	if strings.HasPrefix(stripped, "D") && len(stripped) > 1 && strings.HasPrefix(name, "-D") {
		stripped = stripped[1:]
	}
	stripped = strings.NewReplacer("[", ".", "]", ".").Replace(stripped)
	return stripped != "" && (credentialName(stripped) || credentialName(name))
}

// pairScan is one run of the pair rule over one text.
type pairScan struct {
	t      []rune
	s      string // the text, for the value shapes read at a --name's value start
	bytes  []int  // the byte offset of each rune of s, and len(s)
	known  map[string]bool
	skip   []pairSpan
	starts []int
	nextAt []int32 // the first '@' at or after each position (see atAhead)
}

type pairSpan struct {
	start, end int
	omission   bool
}

// atAhead is the first '@' at or after k, or len(t): read once per text, only when a "://" needs it.
func (p *pairScan) atAhead(k int) int {
	if p.nextAt == nil {
		p.nextAt = make([]int32, len(p.t)+1)
		p.nextAt[len(p.t)] = int32(len(p.t))
		for j := len(p.t) - 1; j >= 0; j-- {
			if p.t[j] == '@' {
				p.nextAt[j] = int32(j)
			} else {
				p.nextAt[j] = p.nextAt[j+1]
			}
		}
	}
	return int(p.nextAt[min(k, len(p.t))])
}

func (p *pairScan) credential(name string) bool {
	known, ok := p.known[name]
	if !ok {
		known = pairName(name)
		p.known[name] = known
	}
	return known
}

// skipped is the end of the span the catalog replaced that holds position, or -1.
func (p *pairScan) skipped(position int) int {
	k := sort.SearchInts(p.starts, position+1) - 1
	if k >= 0 && position < p.skip[k].end {
		return p.skip[k].end
	}
	return -1
}

// separatorPair is the separator branch's decision for the separator (of size) at i, the name read back no further than floor:
// pair is true when the name is a credential name or undecided (a gap that is not readable, a name past its window, or a name
// that ends at a percent escape and stays undecided after one decode).
func (p *pairScan) separatorPair(i, size, floor int) (pair bool, name string, overflow bool) {
	t := p.t
	nameEnd := gapStart(t, i, floor)
	readable := readableGap(t, nameEnd, i)
	nameStart := nameEnd
	for nameStart > floor && nameEnd-nameStart < pairNameCap && pairNameChar(t[nameStart-1]) && !escapeBefore(t, nameStart, floor) {
		nameStart--
	}
	overflow = nameEnd-nameStart >= pairNameCap && nameStart > floor && pairNameChar(t[nameStart-1]) && !escapeBefore(t, nameStart, floor)
	if overflow {
		pathBudget(string(t[nameStart:nameEnd]))
	}
	var decoded string
	var undecided, escaped bool
	if !overflow {
		decoded, undecided, escaped = escapedName(t, nameStart, nameEnd, floor)
	}
	plain := string(t[nameStart:nameEnd])
	name = plain
	if escaped {
		name = decoded
	}
	pair = nameEnd-nameStart >= 2 && (overflow || !readable || p.credential(plain)) ||
		escaped && (undecided || !readable || p.credential(decoded))
	return pair, name, overflow
}

// flagAhead reports another flag at start, so that a --name before it has no value word: a "--", or a single-dash flag with a
// full credential name of two or more characters, then plain whitespace and more text.
func (p *pairScan) flagAhead(start int) bool {
	t := p.t
	dashes := flagDashes(t, start)
	if dashes != 1 {
		return dashes == 2
	}
	end := start + 1
	for end < len(t) && pairNameChar(t[end]) && end-start <= pairNameCap {
		end++
	}
	if end-start < 3 || end-start > pairNameCap || end >= len(t) || !pySpace(t[end]) {
		return false
	}
	after := end
	for after < len(t) && pySpace(t[after]) {
		after++
	}
	return after < len(t) && p.credential(string(t[start:end]))
}

// lineStartsPair reports that the line read fresh at start begins with a pair, by any rule: a name (bare, dashed, bracketed, with
// %XX escapes other than encoded whitespace, or quoted in any spelling), a gap and a separator; or another flag.
func (p *pairScan) lineStartsPair(start int) bool {
	t := p.t
	length, k := len(t), -1
	for _, quote := range []string{entityQuote, escapedQuote, `"`, "'"} {
		if runesAt(t, start, quote) {
			closing := runesFind(t, quote, start+len(quote), min(length, start+len(quote)+130))
			if closing < 0 {
				return false
			}
			k = closing + len(quote)
			break
		}
	}
	if k < 0 {
		if p.flagAhead(start) {
			return true
		}
		name := start
		for name < length && t[name] == '-' && name-start < 2 {
			name++
		}
		k = name
		for k < length && k-name < pairNameCap {
			if pairNameChar(t[k]) {
				k++
			} else if k+3 <= length && escapeBefore(t, k+3, k) && !encodedEqualsDigits(t, k+1) && encodedSpaceAhead(t, k) == 0 {
				k += 3
			} else {
				break
			}
		}
		if k == name {
			return false
		}
	}
	k = gapAhead(t, k)
	return k < length && separatorAt(t, k) != 0
}

// quotedPair reports that a quoted name is a credential or undecided pair, and gives the name: a name past its window is
// undecided; a name that ends at a percent escape is decoded once, undecided when a %XX remains or it holds nothing.
func (p *pairScan) quotedPair(q quotedName) (bool, string) {
	name := ""
	if q.opening >= 0 {
		name = string(p.t[q.opening+len(q.quote) : q.close])
	}
	if q.overflow || p.credential(name) {
		return true, name
	}
	if q.opening < 0 {
		return false, name
	}
	n := []rune(name)
	plain := len(n)
	for plain > 0 && pairNameChar(n[plain-1]) && !escapeBefore(n, plain, 0) {
		plain--
	}
	decoded, undecided, escaped := escapedName(n, plain, len(n), 0)
	return escaped && (undecided || p.credential(decoded)), name
}

// equalsValueEnd is the value end of a quoted name with '=' or %3D at k.
func (p *pairScan) equalsValueEnd(k, size int, name string) int {
	t := p.t
	value := k + size
	for value < len(t) && value-k-size < 8 && t[value] == ' ' {
		value++
	}
	if pairListHeaders[privacyRules.normalize(name)] {
		return lineEnd(t, value)
	}
	return pairValueEnd(t, value)
}

// decide hands a separator inside or at the end of consumed text (read back no further than floor) to the branch that reads it:
// the end of that pair's value when it is a credential or undecided pair (k itself when readEnd is false), else -1.
func (p *pairScan) decide(k, size, floor int, readEnd bool) int {
	t := p.t
	if q, ok := jsonName(t, k); ok && q.opening >= floor {
		pair, name := p.quotedPair(q)
		if size == 1 && t[k] == ':' {
			if !pair {
				return -1
			}
			if !readEnd {
				return k
			}
			value := k + 1
			for value < len(t) && javaSpace(t[value]) {
				value++
			}
			return jsonValueEnd(t, value, pairListHeaders[privacyRules.normalize(name)], q.quote)
		}
		if pair {
			if readEnd {
				return p.equalsValueEnd(k, size, name)
			}
			return k
		}
	}
	if pair, _, _ := p.separatorPair(k, size, floor); !pair {
		return -1
	}
	if readEnd {
		return separatorValueEnd(t, k, size)
	}
	return k
}

// quotedEquals reports that the '=' or %3D at k follows a quoted name that is a credential or undecided pair.
func (p *pairScan) quotedEquals(k int) bool {
	if k <= 0 {
		return false
	}
	q, ok := jsonName(p.t, k)
	if !ok {
		return false
	}
	pair, _ := p.quotedPair(q)
	return pair
}

// shapeEnd is the end of the longest catalog value shape that starts at value, or value.
func (p *pairScan) shapeEnd(value int) int {
	end := value
	from := p.bytes[value]
	for k, shape := range privacyRules.wordShapes {
		// A shape that ends with \b ends where Python's word boundary holds (shapeRuns).
		if m := shape.FindStringIndex(p.s[from:]); m != nil && (!privacyRules.shapeBounds[k][1] || pyBoundary(p.s, from+m[1])) {
			end = max(end, value+utf8.RuneCountInString(p.s[from:from+m[1]]))
		}
	}
	return end
}

// wordEnd is the end of a --name's value word that starts at value; the replacement starts at begin. The word ends at any
// whitespace; a quoted word runs to its closing quote and on to the word end; a value shape that starts at value is the value
// whole. Each separator in the word or at its end is handed to the branch that reads it; for a credential or undecided pair the
// replacement runs on to that pair's value end.
func (p *pairScan) wordEnd(begin, value int) int {
	t := p.t
	length := len(t)
	valueEnd, word := value, value
	if t[value] == '"' || t[value] == '\'' {
		valueEnd = value + 1
		for valueEnd < length && t[valueEnd] != t[value] {
			if t[valueEnd] == '\\' {
				valueEnd += 2
			} else {
				valueEnd++
			}
		}
		valueEnd = min(length, valueEnd+1)
		word = valueEnd
	}
	shapeEnd := p.shapeEnd(value)
	first, last := -1, -1
	for valueEnd < length && (valueEnd < shapeEnd || !pySpace(t[valueEnd])) {
		size := 0
		if valueEnd >= word {
			size = separatorAt(t, valueEnd)
		}
		if size != 0 && p.decide(valueEnd, size, begin, false) >= 0 {
			if first < 0 {
				first = valueEnd
			}
			last = valueEnd
		}
		valueEnd++
	}
	// A separator after the word and a gap on its line is read too: the word is that pair's name.
	if ahead := gapAhead(t, valueEnd); ahead > valueEnd && ahead < length {
		if size := separatorAt(t, ahead); size != 0 && p.decide(ahead, size, begin, false) >= 0 {
			if first < 0 {
				first = ahead
			}
			last = ahead
		}
	}
	if first >= 0 {
		valueEnd = max(valueEnd, p.decide(first, separatorAt(t, first), begin, true))
	}
	if last >= 0 && last != first {
		valueEnd = max(valueEnd, p.decide(last, separatorAt(t, last), begin, true))
	}
	return consumedEnd(t, begin, valueEnd, p.decide)
}

// credentialPairs replaces the value of every credential-named pair with the placeholder, in one left-to-right pass. skip holds
// the spans (rune positions) the catalog itself replaced in a whole URL value: a pair whose value starts inside one is left as the
// catalog wrote it. An omission span shields nothing.
func credentialPairs(s string, skip []pairSpan) string {
	t := []rune(s)
	p := &pairScan{t: t, s: s, known: map[string]bool{}}
	p.bytes = make([]int, 0, len(t)+1)
	for at := range s {
		p.bytes = append(p.bytes, at)
	}
	p.bytes = append(p.bytes, len(s))
	for _, span := range skip {
		if !span.omission {
			p.skip = append(p.skip, span)
			p.starts = append(p.starts, span.start)
		}
	}
	length, copied, i := len(t), 0, 0
	dashesColon := -1
	var out strings.Builder
	changed := false
	for i < length {
		c := t[i]
		start, stop, replacement, userinfo := -1, -1, CredentialRedacted, false
		var quoted quotedName
		isQuoted := false
		if c == ':' && i > 0 {
			quoted, isQuoted = jsonName(t, i)
		}
		switch {
		case isQuoted:
			pair, name := p.quotedPair(quoted)
			if pair {
				value := i + 1
				for value < length && javaSpace(t[value]) {
					value++
				}
				if len(p.skip) > 0 && p.skipped(value) >= 0 {
					i = max(p.skipped(value), i+1)
					continue
				}
				header := pairListHeaders[privacyRules.normalize(name)]
				end := jsonValueEnd(t, value, header, quoted.quote)
				if !header {
					end = consumedEnd(t, value, end, p.decide)
				}
				if end > value {
					start, stop, replacement = value, end, quoted.quote+CredentialRedacted+quoted.quote
				}
			}
			if start < 0 {
				i++
				continue
			}
		case c == ':' && runesAt(t, i+1, "//") && i != dashesColon:
			limit, end := min(length, i+3+512), i+3
			for end < limit && !strings.ContainsRune("/?# \t\r\n", t[end]) {
				end++
			}
			at := runeLastIndex(t, '@', i+3, end)
			if at < i+3 && p.atAhead(i+3) < min(length, i+1024) {
				// A password may hold '/', '?' or '#': a user:password form before a later '@' in the same word runs to it.
				word := i + 3
				for word < length && word-i < 1024 && !javaSpace(t[word]) {
					word++
				}
				later := runeLastIndex(t, '@', i+3, word)
				colon := -1
				if later > i+3 {
					colon = runeIndex(t, ':', i+3, later)
				}
				if later > i+3 && colon > i+3 {
					at = later
				}
			}
			if at < i+3 {
				i += 3
				continue
			}
			start, stop, userinfo = i+3, at, true
		case (c == '=' || encodedEquals(t, i)) && p.quotedEquals(i):
			// A quoted credential or undecided name with '=' or %3D: the value runs to its true end.
			size := 1
			if c != '=' {
				size = 3
			}
			value := i + size
			for value < length && value-i-size < 8 && t[value] == ' ' {
				value++
			}
			if len(p.skip) > 0 && p.skipped(value) >= 0 {
				i = max(p.skipped(value), i+1)
				continue
			}
			q, _ := jsonName(t, i)
			_, name := p.quotedPair(q)
			start = value
			stop = p.equalsValueEnd(i, size, name)
			if !pairListHeaders[privacyRules.normalize(name)] {
				stop = consumedEnd(t, value, stop, p.decide)
			}
		case c == '=' || c == ':' || encodedEquals(t, i):
			// The separator: '=', ':', or %3D (%3d). The gap before it is read back whole.
			size := 1
			if c == '%' {
				size = 3
			}
			pair, name, overflow := p.separatorPair(i, size, copied)
			if pair {
				value := i + size
				for value < length && value-i-size < 8 && t[value] == ' ' {
					value++
				}
				if len(p.skip) > 0 && p.skipped(value) >= 0 {
					i = max(p.skipped(value), i+1)
					continue
				}
				start = value
				if !overflow && pairListHeaders[privacyRules.normalize(name)] {
					stop = lineEnd(t, value)
				} else {
					stop = consumedEnd(t, value, pairValueEnd(t, value), p.decide)
				}
			}
		case c == '-' && (i == 0 || pySpace(t[i-1]) || encodedSpaceWidth(t, i, 0) > 0) && flagDashes(t, i) > 0:
			var next bool
			start, stop, next = p.dashesName(i, &dashesColon, &i)
			if next {
				continue
			}
		}
		if start >= 0 && len(p.skip) > 0 {
			// User information that runs on past the catalog's own (to a later '@') is replaced whole, the span with it.
			if spanEnd := p.skipped(start); spanEnd >= 0 && !(userinfo && stop > spanEnd) {
				i = max(spanEnd, i+1)
				continue
			}
		}
		if start >= copied && stop >= start {
			out.WriteString(string(t[copied:start]))
			out.WriteString(replacement)
			changed = true
			copied = stop
			i = max(stop, i+1)
			continue
		}
		i++
	}
	if !changed {
		return s
	}
	out.WriteString(string(t[copied:]))
	return out.String()
}

// dashesName reads a --name, or a single-dash flag with a full credential name, at i. It returns the span to replace, or sets
// *next (the position to read on from) and returns next=true; start -1 with next false moves on past the name.
func (p *pairScan) dashesName(i int, dashesColon, next *int) (start, stop int, moved bool) {
	t := p.t
	length := len(t)
	dashes := flagDashes(t, i)
	end := i + dashes
	for end < length && pairNameChar(t[end]) && end-i < pairNameCap+dashes {
		end++
	}
	overflow := end-i >= pairNameCap+dashes && end < length && pairNameChar(t[end])
	for overflow && end < length && pairNameChar(t[end]) {
		end++
	}
	if overflow {
		pathBudget(string(t[i:end]))
	}
	minimum := 2
	if dashes == 2 {
		minimum = 1
	}
	start, stop = -1, -1
	if end < length && end-i-dashes >= minimum && (dashesSpace(t[end]) || lineBreak(t[end]) || encodedSpaceAhead(t, end) > 0) &&
		(overflow || p.credential(string(t[i:end]))) {
		value, control, gapEnd, spaced := end, -1, end, false
		for value < length {
			if dashesSpace(t[value]) {
				value++
				spaced = spaced || control >= 0
				continue
			}
			width := encodedSpaceAhead(t, value)
			if width == 0 {
				break
			}
			if control < 0 && encodedControl(t, value, width) {
				control = value
			}
			value += width
			gapEnd = value
		}
		if dashesSeparator(t, value) {
			// A separator after the gap: the pair is name<gap>=value, read at the separator (its gap rule included).
			*dashesColon = -1
			if t[value] == ':' {
				*dashesColon = value
			}
			*next = value
			return -1, -1, true
		}
		begin, valueEnd := value, -1
		if control >= 0 {
			begin = control
		}
		switch {
		case value < length && lineBreak(t[value]):
			// After ONE line break the next line is read fresh. A pair that starts it is read by its own branch; else its first
			// word is the value. A blank line ends the --name.
			line := value + 1
			if runesAt(t, value, "\r\n") {
				line = value + 2
			}
			encoded := false
			for line < length {
				if dashesSpace(t[line]) {
					line++
					continue
				}
				if encoded && lineBreak(t[line]) {
					if runesAt(t, line, "\r\n") {
						line += 2
					} else {
						line++
					}
					encoded = false
					continue
				}
				width := encodedSpaceAhead(t, line)
				if width == 0 {
					break
				}
				if control < 0 && encodedControl(t, line, width) {
					control = line
				}
				line, encoded = line+width, true
			}
			if line >= length || lineBreak(t[line]) || dashesSeparator(t, line) {
				if line >= length || lineBreak(t[line]) {
					*next = end
				} else {
					*dashesColon = -1
					if t[line] == ':' {
						*dashesColon = line
					}
					*next = line
				}
				return -1, -1, true
			}
			if p.lineStartsPair(line) {
				if control >= 0 {
					start, stop = control, gapEnd
				} else {
					*next = line
					return -1, -1, true
				}
			} else {
				begin = line
				if control >= 0 {
					begin = control
				}
				valueEnd = p.wordEnd(begin, line)
			}
		case control >= 0 && !spaced && value < length:
			// An encoded control right before the word starts it: the word runs from the control to its end.
			valueEnd = p.wordEnd(begin, value)
		case value >= length || p.flagAhead(value):
			// No word, or the next word is another flag.
			if control >= 0 {
				start, stop = control, gapEnd
			}
		default:
			valueEnd = p.wordEnd(begin, value)
		}
		if valueEnd > begin {
			start, stop = begin, valueEnd
		}
	}
	if start < 0 {
		*next = end
		return -1, -1, true
	}
	return start, stop, false
}

// vocabulary reports that a segment of the name is one of the catalog's vocabulary words. A segment is split at every UTF-16
// unit that is not a letter or decimal digit (a character outside the BMP is two such units) and at a lowercase letter followed
// by an uppercase one; words compare in lowercase.
func vocabulary(name string) bool {
	p := privacyRules
	lowered := strings.ToLower(name)
	present := false
	for word := range p.words {
		present = present || strings.Contains(lowered, word)
	}
	if !present {
		return false
	}
	var word []rune
	var previous rune = -1
	check := func() bool {
		found := len(word) > 0 && p.words[string(word)]
		word = word[:0]
		return found
	}
	for _, c := range name + "-" {
		letter := c <= 0xFFFF && !(c >= 0xD800 && c <= 0xDFFF) && (unicode.IsLetter(c) || unicode.Is(unicode.Nd, c))
		boundary := !letter || previous >= 0 && unicode.IsUpper(c) && unicode.IsLower(previous)
		if boundary && len(word) > 0 && check() {
			return true
		}
		if letter {
			word = append(word, unicode.ToLower(c))
		}
		previous = c
		if !letter && (c > 0xFFFF || c >= 0xD800 && c <= 0xDFFF) {
			previous = -1
		}
	}
	return false
}
