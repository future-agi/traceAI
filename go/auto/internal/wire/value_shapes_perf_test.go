package wire

import (
	"math/rand"
	"regexp"
	"strings"
	"testing"
	"time"
	"unicode"
	"unicode/utf8"
)

// Shared adversarial-input sizes and budget; reuse across SDK ports.
const (
	shapeAdversarialRepeat = 10000
	shapeBudget            = 100 * time.Millisecond
	shapeBestOf            = 3
	// Under the race detector wall-clock time is several times slower and varies with the machine, so the
	// scan is judged by how its time grows when the input doubles.
	shapeMaxGrowth   = 3.0
	shapeGrowthFloor = 10 * time.Millisecond
)

// Each case is a repeated start that stresses one catalog value shape; build(n) repeats it n times the base size.
// The public scrubber entry point is PrivacyString.
var shapeAdversarialInputs = []struct {
	name  string
	build func(n int) string
}{
	{"jwt-eyJ-dash", func(n int) string { return strings.Repeat("eyJ-", shapeAdversarialRepeat*n) }},
	{"jwt-eyJ-dot-eyJ-dash-two-dots", func(n int) string { return strings.Repeat("eyJ.eyJ-", 5000*n) }},
	{"jwt-eyJ-dash-then-dot", func(n int) string { return strings.Repeat("eyJ-", shapeAdversarialRepeat*n) + "." }},
	{"jwt-eyJ-two-segments", func(n int) string {
		return strings.Repeat("eyJ-", shapeAdversarialRepeat*n) + "." + strings.Repeat("a", 20)
	}},
	{"provider-key-sk", func(n int) string { return strings.Repeat("sk-", shapeAdversarialRepeat*n) }},
	{"provider-key-sk-unterminated", func(n int) string { return "sk-" + strings.Repeat("a", shapeAdversarialRepeat*3*n) + "!" }},
	{"aws-akia", func(n int) string { return strings.Repeat("AKIA", shapeAdversarialRepeat*n) }},
	{"aws-asia-short", func(n int) string { return strings.Repeat("ASIA"+"A", shapeAdversarialRepeat*n) }},
	{"authorization-bearer", func(n int) string { return strings.Repeat("Bearer ", shapeAdversarialRepeat*n) }},
	{"authorization-basic-short", func(n int) string { return strings.Repeat("Basic abc ", shapeAdversarialRepeat*n) }},
	{"private-key-begin", func(n int) string { return strings.Repeat("-----BEGIN PRIVATE KEY-----", shapeAdversarialRepeat*n) }},
	{"private-key-begin-rsa-newline", func(n int) string {
		return strings.Repeat("-----BEGIN RSA PRIVATE KEY-----\n", shapeAdversarialRepeat*n)
	}},
}

// bestOfScrub is the fastest of shapeBestOf runs, so one slow run (a garbage collection, a busy machine) does not decide.
func bestOfScrub(in string) time.Duration {
	best := time.Duration(1<<63 - 1)
	for i := 0; i < shapeBestOf; i++ {
		start := time.Now()
		PrivacyString(in, nil)
		if elapsed := time.Since(start); elapsed < best {
			best = elapsed
		}
	}
	return best
}

func TestValueShapesBoundedTime(t *testing.T) {
	PrivacyString("warm up eyJ-.sk-Bearer AKIA", nil) // warm up lazy init
	for _, tc := range shapeAdversarialInputs {
		t.Run(tc.name, func(t *testing.T) {
			input := tc.build(1)
			elapsed := bestOfScrub(input)
			t.Logf("best_of_%d_elapsed_ms=%.1f input_len=%d race=%v", shapeBestOf, float64(elapsed.Microseconds())/1000, len(input), raceEnabled)
			if raceEnabled {
				doubled := bestOfScrub(tc.build(2))
				t.Logf("doubled_elapsed_ms=%.1f growth=%.2f", float64(doubled.Microseconds())/1000, float64(doubled)/float64(elapsed))
				base := elapsed
				if base < shapeGrowthFloor {
					base = shapeGrowthFloor
				}
				if float64(doubled) >= shapeMaxGrowth*float64(base) {
					t.Fatalf("doubling the input took %v after %v: time is not linear in the text", doubled, elapsed)
				}
				return
			}
			if elapsed > shapeBudget {
				t.Fatalf("scrub took %v, budget %v", elapsed, shapeBudget)
			}
		})
	}
}

// Real-looking JWTs (fake signatures) are replaced whole.
func TestValueShapesJWTFixturesReplacedWhole(t *testing.T) {
	header := "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
	payload := "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUiLCJpYXQiOjE1MTYyMzkwMjJ9"
	jwt := header + "." + payload + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
	dashEnd := header + "." + payload + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQss-"
	underscoreEnd := header + "." + payload + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQss_"
	cases := []struct{ name, in, want string }{
		{"bare", jwt, CredentialRedacted},
		{"signature-ends-dash", dashEnd, CredentialRedacted},
		{"signature-ends-underscore", underscoreEnd, CredentialRedacted},
		{"preceded-by-dash", "x-" + jwt, "x-" + CredentialRedacted},
		{"in-text", "token " + jwt + " end", "token " + CredentialRedacted + " end"},
		{"in-text-dash-sig", "token " + dashEnd + " end", "token " + CredentialRedacted + " end"},
		{"quoted", `{"t":"` + jwt + `"}`, `{"t":"` + CredentialRedacted + `"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := PrivacyString(tc.in, nil); got != tc.want {
				t.Fatalf("got %q want %q", got, tc.want)
			}
		})
	}
}

// Large real JWTs must be redacted whole: a header over 1024 chars (x5c-like)
// or a payload over 8192 chars must not leave any token characters behind.
func TestValueShapesLargeJWTsReplacedWhole(t *testing.T) {
	sig := "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
	normalHeader := "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
	normalPayload := "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUiLCJpYXQiOjE1MTYyMzkwMjJ9"
	bigHeader := "eyJ4NWMiOlsi" + strings.Repeat("MIIBkTCB-_", 200)     // ~2 KB
	bigPayload := "eyJkYXRhIjoi" + strings.Repeat("QUJDREVGR0g-", 1000) // ~12 KB
	cases := []struct{ name, tok string }{
		{"2KB-header", bigHeader + "." + normalPayload + "." + sig},
		{"12KB-payload", normalHeader + "." + bigPayload + "." + sig},
	}
	for _, tc := range cases {
		for _, prefix := range []string{"", "Bearer "} {
			t.Run(tc.name+"/prefix="+strings.TrimSpace(prefix), func(t *testing.T) {
				in := prefix + tc.tok
				want := CredentialRedacted
				if prefix != "" {
					// the Bearer authorization shape also applies; both end in the marker
					want = prefix + CredentialRedacted
				}
				got := PrivacyString(in, nil)
				if got != CredentialRedacted && got != want {
					t.Fatalf("not redacted whole: len(in)=%d got=%q", len(in), truncate(got))
				}
				if strings.Contains(got, "eyJ") || strings.Contains(got, sig) {
					t.Fatalf("token characters left: %q", truncate(got))
				}
				if got2 := redactValueShapes(in); got2 != want && got2 != CredentialRedacted {
					t.Fatalf("value-shape function: got %q", truncate(got2))
				}
			})
		}
	}
}

func truncate(s string) string {
	if len(s) > 120 {
		return s[:120] + "..."
	}
	return s
}

// Differential test against the plain unbounded jwt regex using Go's regexp.
// Strings built from the pieces cannot trigger any other value shape. Besides
// random pieces it feeds near-misses of a valid token: one character replaced
// by a non-run character, a dot removed or added, runs mixing '.', '-' and '_',
// and a non-ASCII letter next to eyJ. The word boundary is the shared reference's (Python 3 re, Unicode: a non-ASCII letter is
// a word character), not Go's ASCII \b, so the plain regex's candidates are kept only where that boundary holds.
func TestValueShapesJWTDifferentialAgainstUnboundedReference(t *testing.T) {
	plain := regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}`)
	ref := unicodeBoundaryRegexp{plain}
	pieces := []string{"eyJ", "eyJaaaaaaaa", "aaaaaaaa", ".", "-", "_", " ", "x", "\u00e9", "\u00e9eyJ", "eyJ-", "a.", ".a", "..", "eyJ.eyJ-"}
	rng := rand.New(rand.NewSource(20260721))
	hits, rounds := 0, 0
	check := func(in string) {
		t.Helper()
		want := ref.ReplaceAllLiteralString(in, CredentialRedacted)
		if want != in {
			hits++
		}
		rounds++
		if got := redactValueShapes(in); got != want {
			t.Fatalf("input %q: got %q want %q", truncate(in), truncate(got), truncate(want))
		}
	}
	for i := 0; i < 20000; i++ {
		var b strings.Builder
		for n := 1 + rng.Intn(20); n > 0; n-- {
			b.WriteString(pieces[rng.Intn(len(pieces))])
		}
		check(b.String())
	}
	valid := "eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeK"
	breakers := []string{"!", " ", "\u00e9", "\n", "+", "/"}
	for _, ctx := range []string{"%s", "x-%s", "x%s", "\u00e9%s", "%s\u00e9", "%s-", "%s.eyJaaaaaaaa", "a.%s.b"} {
		wrap := func(v string) string { return strings.Replace(ctx, "%s", v, 1) }
		check(wrap(valid))
		for pos := 0; pos < len(valid); pos++ {
			for _, br := range breakers {
				check(wrap(valid[:pos] + br + valid[pos+1:]))
			}
			check(wrap(valid[:pos] + valid[pos+1:]))
			for _, add := range []string{".", "-", "_", ".."} {
				check(wrap(valid[:pos] + add + valid[pos:]))
			}
		}
	}
	runChars := []byte("eyJ.-_a0")
	for i := 0; i < 8000; i++ {
		n := 1 + rng.Intn(60)
		b := make([]byte, 0, n+4)
		if rng.Intn(2) == 0 {
			b = append(b, "eyJ"...)
		}
		for j := 0; j < n; j++ {
			if rng.Intn(6) == 0 {
				b = append(b, "eyJ"...)
			}
			b = append(b, runChars[rng.Intn(len(runChars))])
		}
		check(string(b))
	}
	// One large input: a 1.5 MB token whose segments need the leftmost-start rule.
	big := strings.Repeat("eyJ-", 50000) + "x" + strings.Repeat("A", 100000) + "." + strings.Repeat("B", 100000) + "." + strings.Repeat("C-", 50000)
	check(big)
	check("\u00e9eyJ" + strings.Repeat("a", 100000) + "." + strings.Repeat("b", 100000) + "." + strings.Repeat("c", 100000))
	t.Logf("rounds=%d rounds_with_redaction=%d large_input_bytes=%d", rounds, hits, len(big))
}

// unicodeBoundaryRegexp replaces the matches of a pattern that starts with \b where Python's Unicode word boundary holds before
// the match: the character before it is no letter, number or '_'. A dropped candidate is retried from its next character.
type unicodeBoundaryRegexp struct{ re *regexp.Regexp }

func (u unicodeBoundaryRegexp) ReplaceAllLiteralString(in, with string) string {
	var out strings.Builder
	last := 0
	for pos := 0; pos <= len(in); {
		loc := u.re.FindStringIndex(in[pos:])
		if loc == nil {
			break
		}
		start, end := pos+loc[0], pos+loc[1]
		before := false
		if start > 0 {
			r, _ := utf8.DecodeLastRuneInString(in[:start])
			before = r == '_' || unicode.IsLetter(r) || unicode.IsNumber(r)
		}
		if !before {
			out.WriteString(in[last:start])
			out.WriteString(with)
			last, pos = end, end
			continue
		}
		_, size := utf8.DecodeRuneInString(in[start:])
		pos = start + size
	}
	out.WriteString(in[last:])
	return out.String()
}
