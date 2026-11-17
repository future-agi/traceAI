package wire

import (
	"strings"
	"testing"
)

func TestQueryJSONEscapeCut(t *testing.T) {
	// Slash-escaped JSON is catalog-recognized too. A value limit can end in
	// the middle of a JSON escape, after JPEG payload bytes have survived.
	for _, escape := range []string{`\/`, `/`} {
		prefix := strings.ReplaceAll(`{"link":"https://example.invalid/?v=/9j/`, "/", escape)
		full := prefix + strings.Repeat(escape, 24) + `"}`
		if got := PrivacyValue(full, "app.record", nil, nil).(string); !strings.Contains(got, "<omitted:") {
			t.Fatalf("unlimited JSON control not recognized: %q", got)
		}
		for n := len(prefix); n < len(full); n++ {
			cut := full[:n]
			risk := MediaRisk{Truncated: func(s string) bool { return len(s) >= n }}
			if got := PrivacyValueFailClosed(cut, "app.record", nil, nil, risk).(string); !strings.Contains(got, "<omitted:") {
				t.Fatalf("JSON cut %d: %q -> %q", n, cut, got)
			}
		}
	}
}
