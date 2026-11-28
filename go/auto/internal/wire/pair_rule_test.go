package wire

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

// pairRuleCase is one export through the privacy walk, as the shared fixture test runs it; the expected values come from the
// shared executable reference (spec/auto-instrumentation/generate_privacy_fixtures.py).
type pairRuleCase struct {
	name, value, path, expected string
	framed                      bool
	secrets                     []string
}

func runPairRuleCases(t *testing.T, cases []pairRuleCase) {
	t.Helper()
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var value, expected any
			if err := json.Unmarshal([]byte(c.value), &value); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal([]byte(c.expected), &expected); err != nil {
				t.Fatal(err)
			}
			got := privacyWalk(value, c.path, c.secrets, newPrivacyBudget(), 0, mediaHint{}, nil, c.framed)
			if !reflect.DeepEqual(got, expected) {
				t.Fatalf("got %s; want %s", dump(got), dump(expected))
			}
			again := privacyWalk(got, c.path, c.secrets, newPrivacyBudget(), 0, mediaHint{}, nil, c.framed)
			if !reflect.DeepEqual(again, got) {
				t.Fatalf("not idempotent: %s -> %s", dump(got), dump(again))
			}
		})
	}
}

// Rule group 1: every credential-named pair is replaced to the true end of its value, known secrets go first, and re-dumped JSON of a structured string is not read again.
func TestPairRuleTrueEnd(t *testing.T) {
	runPairRuleCases(t, []pairRuleCase{
		{name: "order", value: `"token=s3cr3tv4lue,user=bob end"`, path: "", framed: false, expected: `"token=<redacted>"`, secrets: []string{"s3cr3tv4lue,user=bob"}},
		{name: "prose", value: `"note password=hunter two, x=1"`, path: "", framed: false, expected: `"note password=<redacted>, x=1"`},
		{name: "colon", value: `"auth_token: abc def\nnext: 1"`, path: "", framed: false, expected: `"auth_token: <redacted>\nnext: 1"`},
		{name: "dashes", value: `"run --password hunter2 --verbose"`, path: "", framed: false, expected: `"run --password <redacted> --verbose"`},
		{name: "structured", value: `"{\"note\": \"password=abc\", \"x\": \"y\"}"`, path: "", framed: true, expected: `"{\"note\":\"password=<redacted>\",\"x\":\"y\"}"`},
		{name: "placeholder", value: `"password=<redacted>tail more"`, path: "", framed: false, expected: `"password=<redacted>"`},
	})
}

// Rule group 2: a whole URL value is read by the pair rule too, skipping only the spans the catalog replaced.
func TestPairRuleWholeURL(t *testing.T) {
	runPairRuleCases(t, []pairRuleCase{
		{name: "whole-url", value: `"https://h.invalid/p?q=x;password=abc&z=1"`, path: "", framed: true, expected: `"https://h.invalid/p?q=x;password=<redacted>"`},
		{name: "whole-url-userinfo", value: `"https://user:pw@h.invalid/p?a=1"`, path: "", framed: true, expected: `"https://<redacted>@h.invalid/p?a=1"`},
	})
}

// Rule group 3: credential-named pairs after '#', after a second '?' and in a path segment ('=' or %3D) are catalog pairs.
func TestPairRuleURLCatalog(t *testing.T) {
	runPairRuleCases(t, []pairRuleCase{
		{name: "fragment", value: `"https://h.invalid/p?a=1#password=xyz&b=2"`, path: "", framed: true, expected: `"https://h.invalid/p?a=1#password=<redacted>&b=2"`},
		{name: "second-q", value: `"https://h.invalid/p?q=a?password=xyz"`, path: "", framed: true, expected: `"https://h.invalid/p?q=a?password=<redacted>"`},
		{name: "path", value: `"https://h.invalid/a/password=xyz/b"`, path: "", framed: true, expected: `"https://h.invalid/a/password=<redacted>/b"`},
		{name: "path-3d", value: `"https://h.invalid/a/password%3Dxyz/b"`, path: "", framed: true, expected: `"https://h.invalid/a/password%3D<redacted>/b"`},
		{name: "prose-url", value: `"see https://h.invalid/a/password=xyz/b next"`, path: "", framed: false, expected: `"see https://h.invalid/a/password=<redacted>"`},
	})
}

// Rule group 4: the catalog vocabulary words are credential names at every site, whole or as a segment.
func TestPairRuleVocabulary(t *testing.T) {
	runPairRuleCases(t, []pairRuleCase{
		{name: "query", value: `"https://h.invalid/p?pwd=xyz&b=1"`, path: "", framed: true, expected: `"https://h.invalid/p?pwd=<redacted>&b=1"`},
		{name: "key", value: `{"db_pin": "1234", "x": "1"}`, path: "", framed: true, expected: `{"db_pin": "<redacted>", "x": "1"}`},
		{name: "prose", value: `"login passcode=9876 ok"`, path: "", framed: false, expected: `"login passcode=<redacted>"`},
		{name: "camel", value: `{"dbPass": "v"}`, path: "", framed: true, expected: `{"dbPass": "<redacted>"}`},
	})
}

// Rule group 5: a quoted credential name's value ends by the quoted-name ruling: a string at its closing quote, a scalar before a member boundary, anything else at its true end.
func TestPairRuleJSONQuotedNames(t *testing.T) {
	runPairRuleCases(t, []pairRuleCase{
		{name: "a", value: `"r {\"password\": \"abc def\", \"x\": 1}"`, path: "", framed: true, expected: `"r {\"password\": \"<redacted>\", \"x\": 1}"`},
		{name: "a-escaped", value: `"r {\\\"password\\\": \\\"abc def\\\", \\\"x\\\": 1}"`, path: "", framed: true, expected: `"r {\\\"password\\\": \\\"<redacted>\\\", \\\"x\\\": 1}"`},
		{name: "b", value: `"r {\"password\": 12345, \"x\": 1}"`, path: "", framed: true, expected: `"r {\"password\": \"<redacted>\", \"x\": 1}"`},
		{name: "c", value: `"r {\"password\": {\"a\": 1}, \"x\": 1}"`, path: "", framed: true, expected: `"r {\"password\": \"<redacted>\""`},
		{name: "c-prose", value: `"r {\"password\": abc def, \"x\": 1}"`, path: "", framed: true, expected: `"r {\"password\": \"<redacted>\""`},
	})
}

// Rule group 6: map keys go through the export text rules; keys that collide after scrubbing keep one key whose value is the placeholder.
func TestPairRuleMapKeys(t *testing.T) {
	runPairRuleCases(t, []pairRuleCase{
		{name: "keys", value: `{"see password=abc": "v", "see password=xyz": "w"}`, path: "", framed: true, expected: `{"see password=<redacted>": "<redacted>"}`},
		{name: "key-url", value: `{"https://h.invalid/?api_key=abc": "v"}`, path: "", framed: true, expected: `{"https://h.invalid/?api_key=<redacted>": "<redacted>"}`},
	})
}

// Rule group 7: a path, key or pair name of more than limits.max_depth components exports the whole value as the placeholder.
func TestPairRuleNameDepth(t *testing.T) {
	runPairRuleCases(t, []pairRuleCase{
		{name: "path", value: `{"a": "x"}`, path: "p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p.p", framed: true, expected: `"<redacted>"`},
		{name: "key", value: `{"k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k.k": "x"}`, path: "", framed: true, expected: `"<redacted>"`},
		{name: "name", value: `"x n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n.n=v"`, path: "", framed: false, expected: `"<redacted>"`},
	})
}

// Rule group 8: a name past its window, or after a '?' holding more than eight '?', is undecided and its pair is replaced.
func TestPairRuleBoundedNames(t *testing.T) {
	runPairRuleCases(t, []pairRuleCase{
		{name: "name", value: `"xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=value"`, path: "", framed: false, expected: `"xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=<redacted>"`},
		{name: "question", value: `"https://h.invalid/p?q=??????????a=b"`, path: "", framed: true, expected: `"https://h.invalid/p?q=??????????a=<redacted>"`},
		{name: "quoted", value: `"\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\": \"v\""`, path: "", framed: false, expected: `"\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\": \"<redacted>\""`},
	})
}

// Rule group 1: a structured string is re-dumped as the shared reference re-dumps it (compact, each object's members in their
// original order, non-ASCII characters as they are, the reference's escapes and number spellings); the pair rule reads its leaves.
func TestPairRuleStructuredRedump(t *testing.T) {
	runPairRuleCases(t, []pairRuleCase{
		{name: "member-order-and-numbers", value: "\"{\\\"z\\\": \\\"password=abc def\\\", \\\"a\\\": 1.0, \\\"b\\\": [1e21, 1.5e-07, -0, 12345678901234567890, 2.50]}\"", framed: true, expected: "\"{\\\"z\\\":\\\"password=<redacted>\\\",\\\"a\\\":1.0,\\\"b\\\":[1e+21,1.5e-07,0,12345678901234567890,2.5]}\""},
		{name: "escapes-and-non-ascii", value: "\"{\\\"m\\\": \\\"keep\\\\u2028line\\\\u0001\\\\b\\\\f\\\\u00e9 <&>\\\", \\\"z\\\": \\\"token=abc\\\"}\"", framed: true, expected: "\"{\\\"m\\\":\\\"keep\u2028line\\\\u0001\\\\b\\\\f\u00e9 <&>\\\",\\\"z\\\":\\\"token=<redacted>\\\"}\""},
	})
}

// A query value with an invalid UTF-8 byte before a second '?' and a credential pair is cut at the right byte.
func TestPairRuleSecondQuestionInvalidUTF8(t *testing.T) {
	for _, value := range []string{"http://a/b?x=\xff?token=", "http://a/b?x=\xff\xfe?token=abc&y=1", "https://a/#x=\xff?pwd=abc"} {
		got, _ := PrivacyValueWithMasker(map[string]any{"k": value}, "", nil, nil).(map[string]any)
		text, _ := got["k"].(string)
		if !strings.Contains(text, CredentialRedacted) || strings.Contains(text, "abc") {
			t.Fatalf("%q -> %q", value, text)
		}
	}
}

// A JSON value followed by more text is no structured string (json.loads rejects it): the text is prose and goes through the
// pair rule.
func TestPairRuleJSONValueFollowedByText(t *testing.T) {
	var cases []pairRuleCase
	for _, framed := range []bool{true, false} {
		cases = append(cases,
			pairRuleCase{name: "empty object then pair", value: `"{} password=TAILV tail"`, expected: `"{} password=<redacted>"`, framed: framed},
			pairRuleCase{name: "array then colon pair", value: `"[1] token: TAILV"`, expected: `"[1] token: <redacted>"`, framed: framed},
			pairRuleCase{name: "object then prose pair", value: `"{\"a\":1} trailing password=TAILV"`, expected: `"{\"a\":1} trailing password=<redacted>"`, framed: framed},
			pairRuleCase{name: "string member then pair", value: `"{\"a\":\"x\"} password=TAILV"`, expected: `"{\"a\":\"x\"} password=<redacted>"`, framed: framed},
			pairRuleCase{name: "object then name with colon", value: `"{}api_keydata:TAILVTAILV"`, expected: `"{}api_keydata:<redacted>"`, framed: framed},
		)
	}
	runPairRuleCases(t, cases)
}

// The value shapes read whitespace as str.isspace does (U+001C-U+001F, U+0085, U+00A0, U+2028 included) and, under the i flag,
// the case variants re.I matches (U+017F, U+0130, U+0131, U+212A).
func TestPairRuleValueShapesUnicode(t *testing.T) {
	var cases []pairRuleCase
	for _, framed := range []bool{true, false} {
		cases = append(cases,
			pairRuleCase{name: "no-break space after the scheme", value: `"Bearer\u00a0abcdefghTAILV"`, expected: `"<redacted>"`, framed: framed},
			pairRuleCase{name: "line separator after the scheme", value: `"x Bearer\u2028abcdefghTAILV y"`, expected: `"x <redacted> y"`, framed: framed},
			pairRuleCase{name: "file separator in the gap", value: `"-Bearer \u001cQUJDtokenTAILV"`, expected: `"-<redacted>"`, framed: framed},
			pairRuleCase{name: "kelvin sign and dotless i in the token", value: `"Bearer \u212a\u0131tokenTAILXQUJD"`, expected: `"<redacted>"`, framed: framed},
			pairRuleCase{name: "dotted capital I ends the token", value: `"Bearer xxxxxxxx\u0130\u00e9\u00e9\u00e9"`, expected: `"<redacted>\u00e9\u00e9\u00e9"`, framed: framed},
			pairRuleCase{name: "long s in the scheme", value: `"Ba\u017fic abcdefgh12345678"`, expected: `"<redacted>"`, framed: framed},
			pairRuleCase{name: "upper case scheme and file separator", value: `"BEARER\u001cabcdefghTAILV"`, expected: `"<redacted>"`, framed: framed},
			pairRuleCase{name: "next line after the scheme", value: `"note Bearer\u0085abcdefghTAILV"`, expected: `"note <redacted>"`, framed: framed},
		)
	}
	runPairRuleCases(t, cases)
}

// Word boundaries in the value shapes are Python 3 re's (Unicode \w), not RE2's ASCII \b, and no data URI starts after a
// character the reference's [A-Za-z0-9+.\-_:] matches under re.I (long s, dotless i).
func TestPairRuleValueShapesUnicodeWordBoundary(t *testing.T) {
	var cases []pairRuleCase
	for _, framed := range []bool{true, false} {
		cases = append(cases,
			pairRuleCase{name: "CJK letter before the scheme", value: `"日本Bearer abcdefghTAILV"`, expected: `"日本Bearer abcdefghTAILV"`, framed: framed},
			pairRuleCase{name: "CJK letter before the scheme then a key block", value: `"x 日本bearer -----BEGIN PRIVATE KEY-----TAILV"`, expected: `"x 日本bearer <redacted>"`, framed: framed},
			pairRuleCase{name: "accented letter before a token", value: `"éeyJaaaaaaaa.aaaaaaaa.aaaaaaaa"`, expected: `"éeyJaaaaaaaa.aaaaaaaa.aaaaaaaa"`, framed: framed},
			pairRuleCase{name: "accented letter after an access key", value: `"AKIAABCDEFGHIJKLMNOPé"`, expected: `"AKIAABCDEFGHIJKLMNOPé"`, framed: framed},
			pairRuleCase{name: "combining mark before a provider key", value: `"́sk-abcdefghijklmnopqrstTAILV"`, expected: `"́<redacted>"`, framed: framed},
			pairRuleCase{name: "long s before data", value: `"ſdata:image/png;base64,QUJD -----BEGIN PRIVATE KEY-----TAILV"`, expected: `"ſdata:image/png;base64,QUJD <redacted>"`, framed: framed},
			pairRuleCase{name: "dotless i before data", value: `"ıdata:,x password=TAILV"`, expected: `"ıdata:,x password=<redacted>"`, framed: framed},
		)
	}
	runPairRuleCases(t, cases)
}

// Known secrets are replaced one at a time, longest first, before value shapes and the pair rule; two overlapping secrets are
// never merged into one span.
func TestPairRuleOverlappingKnownSecretsOneAtATime(t *testing.T) {
	overlapping := []string{"SECRETAAAABBBB", "AAAABBBBCCCC"}
	var cases []pairRuleCase
	for _, framed := range []bool{true, false} {
		cases = append(cases,
			pairRuleCase{name: "prose before a pair", value: `"note SECRETAAAABBBBCCCC password=TAILV"`, expected: `"note <redacted>CCCC password=<redacted>"`, framed: framed, secrets: overlapping},
			pairRuleCase{name: "after a credential name", value: `"token SECRETAAAABBBBCCCC tail"`, expected: `"token <redacted>CCCC tail"`, framed: framed, secrets: overlapping},
			pairRuleCase{name: "whole URL", value: `"https://h.invalid/x?a=SECRETAAAABBBBCCCC&token=TAILV"`, expected: `"https://h.invalid/x?a=<redacted>CCCC&token=<redacted>"`, framed: framed, secrets: overlapping},
			pairRuleCase{name: "credential pair value", value: `"api_key=AAAABBBBCCCCSECRET more"`, expected: `"api_key=<redacted>"`, framed: framed, secrets: []string{"AAAABBBBCCCC", "BBBBCCCCSECRET"}},
		)
	}
	runPairRuleCases(t, cases)
}

// The private key shape runs to its END line or to the end of the text, where Python's $ also holds before a final newline: the
// newline stays.
func TestPairRuleKeyBlockEndsBeforeFinalNewline(t *testing.T) {
	var cases []pairRuleCase
	for _, framed := range []bool{true, false} {
		cases = append(cases,
			pairRuleCase{name: "open block then final newline", value: `"x -----BEGIN PRIVATE KEY-----TAILV\n"`, expected: `"x <redacted>\n"`, framed: framed},
			pairRuleCase{name: "closed block then final newline", value: `"-----BEGIN PRIVATE KEY-----\nTAILV\n-----END PRIVATE KEY-----\n"`, expected: `"<redacted>\n"`, framed: framed},
		)
	}
	runPairRuleCases(t, cases)
}
