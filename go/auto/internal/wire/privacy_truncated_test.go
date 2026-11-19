package wire

import (
	"encoding/base64"
	"fmt"
	"strings"
	"testing"
)

func TestFailClosedLexicalRuns(t *testing.T) {
	for _, payload := range []string{
		"AQIDBA==", "AbCdEf01", "AbCdEf012", "AbCdEf0123", "AbCdEf01234",
		"abcdabcd", "12345678", "_-ab01+z/", "AQID\r\nBA==",
	} {
		for _, frame := range []string{payload, `"` + payload, `failed: "` + payload + `"`, `{"args":"{\"blob\":\"` + payload + `\"}"}`} {
			got := truncatedPrivacyText(frame)
			if strings.Contains(got, payload) || !strings.Contains(got, omitted(payload)) {
				t.Errorf("cut run survived: %q -> %q", frame, got)
			}
			if twice := truncatedPrivacyText(got); twice != got {
				t.Errorf("unstable omission: %q -> %q", got, twice)
			}
		}
	}
	for _, ordinary := range []string{"two words, 123, id.42!", "model=keep", "hello", "id.éclair!", "<redacted> <omitted: 123456789 base64 chars>"} {
		if got := truncatedPrivacyText(ordinary); got != ordinary {
			t.Errorf("non-base64 text changed: %q -> %q", ordinary, got)
		}
	}
}

// Masking a secret can split a larger base64 run. Its short surroundings may
// survive, but every remaining 8+ character run must still be omitted, and the
// redaction marker must stay intact even if its replacement changed the length.
func TestFailClosedMaskBeforeLexicalScan(t *testing.T) {
	const secret = "AbCdEf012"
	for n := 0; n <= 12; n++ {
		left, right := strings.Repeat("Z", n), strings.Repeat("y", n)
		value := "failed: " + left + secret + right
		mask := func(s string) string { return strings.ReplaceAll(s, secret, CredentialRedacted) }
		risk := MediaRisk{Truncated: func(s string) bool { return s == value }}
		got := PrivacyValueFailClosed(value, "app.note", nil, mask, risk)
		if n >= 8 {
			left, right = omitted(left), omitted(right)
		}
		if want := "failed: " + left + CredentialRedacted + right; got != want {
			t.Fatalf("surrounding run %d: got %q, want %q", n, got, want)
		}
	}
}

func TestFailClosedDataURIFragments(t *testing.T) {
	full := "data:audio/L16;base64,AQIDBA=="
	for n := len("data:"); n <= len(full); n++ {
		for _, frame := range []string{full[:n], `failed: "` + full[:n], `{"args":"{\"blob\":\"` + full[:n]} {
			got := PrivacyValueFailClosed(frame, "app.note", nil, nil, MediaRisk{Truncated: func(s string) bool { return s == frame }})
			if strings.Contains(got.(string), "data:") || !strings.Contains(got.(string), "<omitted:") {
				t.Errorf("cut data URI survived: %q -> %q", frame, got)
			}
		}
	}
	for _, complete := range []string{"data:", "data:audio/L16", "metadata:x", "this is plain text"} {
		if got := PrivacyValue(complete, "app.note", nil, nil); got != complete {
			t.Errorf("complete catalog behavior changed: %q -> %q", complete, got)
		}
	}
}

// A partial final quantum must not invalidate a full magic signature in earlier
// bytes, even without a provider length-limit flag. Exercise every catalog rule,
// every cut retaining its complete signature, both alphabets and CR/LF wrapping.
func TestMagicMediaPartialQuantum(t *testing.T) {
	for r, rule := range privacyRules.magic {
		raw := []byte(strings.Repeat("0", privacyRules.Media.PrefixBytes+16))
		signatureBytes := 0
		for _, test := range rule {
			copy(raw[test.offset:], test.expected)
			signatureBytes = max(signatureBytes, test.offset+len(test.expected))
		}
		for _, enc := range []*base64.Encoding{base64.StdEncoding, base64.URLEncoding} {
			full := enc.EncodeToString(raw)
			for n := (signatureBytes*8 + 5) / 6; n < len(full); n++ {
				prefix := full[:n]
				for _, value := range []string{prefix, prefix[:2] + "\r\n" + prefix[2:]} {
					if !magicMedia(value) {
						t.Fatalf("rule %d, cut %d: complete magic not recognized: %q", r, n, value)
					}
					if got := PrivacyValue(value, "app.blob", nil, nil); got != omitted(value) {
						t.Fatalf("rule %d, cut %d: export kept %q", r, n, got)
					}
				}
			}
		}
	}
}

// Cut state follows nested decoded strings independently of relative key names,
// scope hints, whether a token itself is cut, and its length modulo four.
func TestFailClosedNestedCutState(t *testing.T) {
	for n := 8; n < 40; n++ {
		payload := strings.Repeat("AbCdEf01", 5)[:n]
		for _, key := range []string{"arguments", "args", "payload"} {
			frame := fmt.Sprintf(`{"%s":"{\"clip\":{\"data\":\"%s\",\"mimeType\":\"audio/L16\"}}","tail":"`, key, payload)
			risk := MediaRisk{Scopes: []string{"gen_ai"}, Truncated: func(s string) bool { return s == frame }}
			got := PrivacyValueFailClosed(frame, "gen_ai.input.messages", nil, nil, risk).(string)
			if strings.Contains(got, payload) || !strings.Contains(got, "<omitted:") {
				t.Fatalf("nested token survived scope/context loss: %q -> %q", frame, got)
			}
		}
	}
}
