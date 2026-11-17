package wire

import "strings"

// truncatedPrivacyText is a lexical, fail-closed boundary, not a reconstruction
// of the JSON/MIME context that a length limit may have destroyed. Every base64
// run of at least eight characters is suspect, even inside quotes, nested JSON
// strings or at EOF, and even when its length cannot be decoded. Pure alphabetic
// or numeric runs are indistinguishable from base64 here; complete values never
// take this path. Short words, punctuation and non-base64 text stay intact.
//
// The caller has charged the text budget. This scan is linear and bounded, skips
// our existing replacement markers, and never invokes application code.
func truncatedPrivacyText(s string) string {
	var out strings.Builder
	last := 0
	for i := 0; i < len(s); {
		if n := markerEnd(s[i:]); n > 0 {
			i += n
			continue
		}
		if prefixFold(s[i:], "data:") && (i == 0 || !schemeChar(s[i-1])) {
			end := i + dataTokenEnd(s[i:])
			replacement, ok := dataURI(s[i:end])
			if !ok {
				// Unlike the complete-value catalog rule, a cut data: fragment
				// need not retain its comma or encoding declaration.
				replacement = privacyRules.Media.DataURI.Other
			}
			out.WriteString(s[last:i])
			out.WriteString(replacement)
			last, i = end, end
			continue
		}
		if !privacyRules.alphabet[s[i]] || s[i] == '=' || s[i] == '\r' || s[i] == '\n' {
			i++
			continue
		}
		start := i
		for i < len(s) && privacyRules.alphabet[s[i]] && s[i] != '=' {
			i++
		}
		// Padding terminates a run; an equals sign between query name/value
		// tokens must not merge two otherwise independent alphabetic runs.
		for padding := 0; padding < 2 && i < len(s) && s[i] == '='; padding++ {
			i++
		}
		if i-start >= 8 {
			out.WriteString(s[last:start])
			out.WriteString(omitted(s[start:i]))
			last = i
		}
	}
	if last == 0 {
		return s
	}
	out.WriteString(s[last:])
	return out.String()
}
