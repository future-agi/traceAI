package wire

import (
	"encoding/base64"
	"strings"
	"testing"
)

// rc2 candidate-2 (2026-10-31): a value-length limit cuts recognized media to
// a prefix without its full signature. A cut prefix consistent with catalog
// magic is media; incompatible decoded bytes and non-base64 text are not.
func TestMagicPrefix(t *testing.T) {
	// "AAAA", "AAAAEG" and the CR/LF-wrapped forms are an ftyp box size with
	// the signature cut off (rc2c): consistent with ftyp, so media.
	for _, hit := range []string{"iVBORw0K", "UklGRjAw", "AAAAEGZ0", "/9j/4AAQ", "SUQzBAAA", "T2dnUwAC", "ZkxhQwAA", "JVBERi0x", "iVBO", "R0lGODlh", "A", "AQ", "AQI", "AAAA", "AAAAEG", "AAAA\r\nEG", "AAAA\r\n\r\n\r\n\r\n\r\nEG", "\r\nAAAA", "A\r\n\r\n\r\n"} {
		if !magicPrefix(hit) {
			t.Errorf("cut magic prefix %q not recognized", hit)
		}
	}
	for _, miss := range []string{"hellohel", "message", "two words", "", "\r\n\r\n", "====", "gpt-4o-m"} {
		if magicPrefix(miss) {
			t.Errorf("%q is not a magic prefix", miss)
		}
	}
	png := base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\n0000000000000000"))
	for n := 1; n <= len(png); n++ {
		if !magicPrefix(png[:n]) {
			t.Fatalf("PNG prefix of %d chars not recognized", n)
		}
	}
}

// rc2c (2026-10-31, rc2 verifier finding 1): completeness of the cut-prefix
// predicate. For every catalog magic rule, in every catalog-accepted base64
// form (standard, URL-safe, CR/LF inserted after each of the first characters,
// MIME-style and doubled wrapping), every non-empty cut that still
// carries a data character is recognized: no cut can remove all evidence
// while leaving catalog-recognized media bytes.
func TestMagicPrefixCompleteness(t *testing.T) {
	for r, rule := range privacyRules.magic {
		raw := []byte(strings.Repeat("0", privacyRules.Media.PrefixBytes+16))
		for _, test := range rule {
			copy(raw[test.offset:], test.expected)
		}
		var forms []string
		for _, enc := range []*base64.Encoding{base64.StdEncoding, base64.URLEncoding} {
			e := enc.EncodeToString(raw)
			forms = append(forms, e)
			for k := 1; k <= 8; k++ {
				forms = append(forms, e[:k]+"\r\n"+e[k:], e[:k]+strings.Repeat("\r\n", 9)+e[k:], "\r\n"+e[:k]+"\n"+e[k:])
			}
			var wrapped, doubled strings.Builder
			for i := 0; i < len(e); i += 4 {
				end := min(i+4, len(e))
				wrapped.WriteString(e[i:end] + "\r\n")
				doubled.WriteString(e[i:end] + "\r\n\r\n")
			}
			forms = append(forms, wrapped.String(), doubled.String())
		}
		for _, full := range forms {
			if !magicMedia(full) {
				t.Fatalf("rule %d: catalog does not recognize the full value %q", r, full)
			}
			for n := 1; n <= len(full); n++ {
				if strings.Trim(full[:n], "\r\n") != "" && !magicPrefix(full[:n]) {
					t.Fatalf("rule %d: cut %q of recognized media %q not recognized", r, full[:n], full)
				}
			}
		}
	}
}

// The fail-closed walk omits every non-empty value under an at-risk
// media-capable path, whatever its length or alphabet, and nothing else.
func TestFailClosedEveryCapableValue(t *testing.T) {
	values := map[string]any{
		"app.transcript.parts.83.data": "AQ",
		"input.id.parts.9.data":        "\x01\x02\x03\x04\x05",
		"a.content":                    "AQID BAUG\tBwgJ",
		"a.samples":                    []any{1.0, 2.0},
		"a.empty.data":                 "",
		"a.transcript":                 "Sure",
		"a.role":                       "AQI",
		"a.marker.data":                "<omitted: media>",
		"partial":                      `{"part":{"data":"AQ","text":"hi"`,
	}
	out := PrivacyValueFailClosed(values, "", nil, nil, MediaRisk{}).(map[string]any)
	for _, key := range []string{"app.transcript.parts.83.data", "input.id.parts.9.data", "a.content", "a.samples"} {
		if s, _ := out[key].(string); !strings.HasPrefix(s, "<omitted: ") {
			t.Errorf("%s kept: %#v", key, out[key])
		}
	}
	for key, want := range map[string]string{"a.empty.data": "", "a.transcript": "Sure", "a.role": "AQI", "a.marker.data": "<omitted: media>"} {
		if out[key] != want {
			t.Errorf("%s changed: %#v", key, out[key])
		}
	}
	if s := out["partial"].(string); strings.Contains(s, `"AQ"`) || !strings.Contains(s, `"hi"`) {
		t.Errorf("partial JSON: %s", s)
	}
	// Scoped risk leaves other subtrees to the normal catalog walk.
	scoped := PrivacyValueFailClosed(map[string]any{"x.parts.0.data": "AQ", "y.content": "AQ"}, "", nil, nil, MediaRisk{Scopes: []string{"x.parts.0"}}).(map[string]any)
	if scoped["x.parts.0.data"] == "AQ" || scoped["y.content"] != "AQ" {
		t.Errorf("scoped risk: %v", scoped)
	}
	// Bracketed keys use the canonical component form.
	bracket := PrivacyValueFailClosed(map[string]any{"x[0].data": "AQ"}, "", nil, nil, MediaRisk{Scopes: []string{"x.0"}}).(map[string]any)
	if bracket["x[0].data"] == "AQ" {
		t.Errorf("bracketed path escaped its scope: %v", bracket)
	}
}

// The fail-closed export walk omits every cut of wrapped ftyp media at an
// arbitrary key and below metadata boundaries; a cut data URI is omitted once
// its comma survives (before that it carries no payload).
func TestFailClosedCutValues(t *testing.T) {
	e := base64.StdEncoding.EncodeToString(append([]byte{0, 0, 0, 0x10, 'f', 't', 'y', 'p'}, []byte(strings.Repeat("0", 32))...))
	for _, media := range []string{e[:4] + "\r\n" + e[4:], e[:4] + strings.Repeat("\r\n", 9) + e[4:], "data:video/mp4;base64," + e} {
		for n := 1; n <= len(media); n++ {
			cut := media[:n]
			for _, key := range []string{"app.arbitrary", "input.id.parts.9.arbitrary", "app.transcript.parts.83.arbitrary"} {
				risk := MediaRisk{Scopes: []string{MediaRiskScope(key)}, Truncated: func(s string) bool { return len(s) >= n }}
				out := PrivacyValueFailClosed(map[string]any{key: cut, "app.note": "two words"}, "", nil, nil, risk).(map[string]any)
				if out["app.note"] != "two words" {
					t.Fatalf("ordinary text changed: %v", out)
				}
				if strings.HasPrefix(media, "data:") && !strings.Contains(cut, ",") {
					continue
				}
				if s, _ := out[key].(string); !strings.HasPrefix(s, "<omitted: ") {
					t.Fatalf("cut %q at %s exported as %q", cut, key, out[key])
				}
			}
		}
	}
}
