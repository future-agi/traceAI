package wire

import (
	"encoding/base64"
	"fmt"
	"net/url"
	"strings"
	"testing"
)

// Every non-empty decoded-character magic prefix remains private inside a query,
// including cuts in percent escapes, URL-safe alphabets and CR/LF wrapping.
func TestRC2fQueryMagicCuts(t *testing.T) {
	for r, rule := range privacyRules.magic {
		raw := []byte(strings.Repeat("0", privacyRules.Media.PrefixBytes+16))
		for _, test := range rule {
			copy(raw[test.offset:], test.expected)
		}
		for _, enc := range []*base64.Encoding{base64.StdEncoding, base64.URLEncoding, base64.RawURLEncoding} {
			e := enc.EncodeToString(raw)
			for _, payload := range []string{e, e[:4] + "\r\n" + e[4:], "\n" + e[:2] + "\n" + e[2:]} {
				full := "https://example.invalid/?v=" + url.QueryEscape(payload)
				if out := PrivacyValue(full, "app.link", nil, nil).(string); !strings.Contains(out, "<omitted:") {
					t.Fatalf("rule %d control not recognized: %q", r, out)
				}
				start := len("https://example.invalid/?v=")
				for n := start + 1; n <= len(full); n++ {
					value := full[:n]
					decoded := percentDecode(value[start:], true)
					if i := strings.LastIndexByte(decoded, '%'); i >= 0 {
						decoded = decoded[:i]
					}
					if strings.Trim(decoded, "\r\n") == "" {
						continue
					}
					for _, frame := range []string{value, "failed " + value, `{"link":"` + value, `{"link":"` + strings.ReplaceAll(value, "/", `\/`)} {
						risk := MediaRisk{Scopes: []string{"app"}, Truncated: func(s string) bool { return len(s) >= len(frame) }}
						got := PrivacyValueFailClosed(frame, "app.link", nil, nil, risk).(string)
						if !strings.Contains(got, "<omitted:") {
							t.Fatalf("rule %d cut %d: %q -> %q", r, n, frame, got)
						}
					}
				}
			}
		}
	}
}

func TestRC2fQueryLostHint(t *testing.T) {
	for _, name := range []string{"content", "data", "parts.0.content.chunk", "id.parts[9].content", "transcript.parts.83.data"} {
		parent := hintParent(name)
		if name == "parts.0.content.chunk" {
			parent = "parts.0"
		}
		hintName := joinPath(parent, "mimeType")
		full := "https://example.invalid/?model=keep&" + name + "=AQIDBA%3D%3D&" + hintName + "=audio%2Fwav#anchor"
		for _, suffix := range []string{"&" + hintName + "=audio%2Fwav#anchor", "&" + hintName + "=au", "&mime", "&"} {
			value := full[:len(full)-len("&"+hintName+"=audio%2Fwav#anchor")] + suffix
			for i, frame := range []string{value, "failed " + value, `{"link":"` + value, `{"link":"` + strings.ReplaceAll(value, "/", `\/`)} {
				t.Run(fmt.Sprintf("%s/%s/%d", name, suffix, i), func(t *testing.T) {
					risk := MediaRisk{Scopes: []string{"app"}, Truncated: func(s string) bool { return len(s) >= len(frame) }}
					out := PrivacyValueFailClosed(frame, "app.link", nil, nil, risk).(string)
					if strings.Contains(out, "AQIDBA") || !strings.Contains(out, "model=keep") {
						t.Fatalf("lost query context: %q -> %q", frame, out)
					}
				})
			}
		}
	}
}

// Count-only degradation cannot cut a query's internal hints. A value-limit
// elsewhere likewise must not change complete ordinary query strings.
func TestRC2fQueryUncutControls(t *testing.T) {
	// No minimum length/alphabet applies once a query's MIME may be missing.
	for _, payload := range []string{"AQ", "%", "%0", "%01%02", "AQID+BA"} {
		value := "https://example.invalid/?content=" + payload
		risk := MediaRisk{Truncated: func(s string) bool { return len(s) >= len(value) }}
		if got := PrivacyValueFailClosed(value, "app.link", nil, nil, risk).(string); !strings.Contains(got, "<omitted:") {
			t.Fatalf("short/binary query payload survived: %q", got)
		}
	}
	value := "https://example.invalid/?content=hello&v=AAAA&model=keep#anchor"
	for _, risk := range []MediaRisk{{}, {Truncated: func(s string) bool { return len(s) >= 1000 }}} {
		if got := PrivacyValueFailClosed(value, "app.link", nil, nil, risk); got != value {
			t.Fatalf("uncut query changed: %q", got)
		}
	}
	cut := "https://example.invalid/?model=keep&content=AQIDBA%3D%3D&mime"
	risk := MediaRisk{Truncated: func(s string) bool { return len(s) >= len(cut) }}
	once := PrivacyValueFailClosed(cut, "app.link", nil, nil, risk)
	if twice := PrivacyValueFailClosed(once, "app.link", nil, nil, risk); twice != once {
		t.Fatalf("query omission not stable: %q -> %q", once, twice)
	}
}
