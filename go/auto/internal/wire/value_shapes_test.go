package wire

import (
	"strings"
	"testing"
)

// Catalog value shapes redact credential-looking values whatever their key
// name: in free text, JSON values and URL query values. The test values are
// assembled at run time so this file holds no key-shaped literal.
func TestValueShapesRedactUnderInnocuousNames(t *testing.T) {
	provider := "sk-" + strings.Repeat("Ab1_", 6)
	shapes := map[string]string{
		"provider-api-key":    provider,
		"aws-access-key":      "AKIA" + strings.Repeat("A1B2", 4),
		"jwt":                 "eyJ" + strings.Repeat("abcd", 3) + "." + strings.Repeat("efgh", 3) + "." + strings.Repeat("ijkl", 3),
		"authorization-value": "Bearer " + strings.Repeat("Zx9", 4),
		"private-key-pem":     "-----BEGIN " + "PRIVATE KEY-----\nMIIB" + strings.Repeat("Q", 12) + "\n-----END " + "PRIVATE KEY-----",
	}
	for name, value := range shapes {
		t.Run(name, func(t *testing.T) {
			if got := PrivacyString("note "+value+" end", nil); got != "note <redacted> end" {
				t.Fatalf("free text kept the credential shape: %q", got)
			}
			body := map[string]any{"message": value, "list": []any{"x " + value}}
			got, ok := PrivacyValue(body, "", nil, nil).(map[string]any)
			if !ok || got["message"] != CredentialRedacted || got["list"].([]any)[0] != "x <redacted>" {
				t.Fatalf("JSON values kept the credential shape: %v", got)
			}
			again := PrivacyValue(got, "", nil, nil).(map[string]any)
			if again["message"] != CredentialRedacted || again["list"].([]any)[0] != "x <redacted>" {
				t.Fatalf("value-shape redaction is not idempotent: %v", again)
			}
		})
	}
	if got := PrivacyString("https://example.invalid/?note="+provider, nil); got != "https://example.invalid/?note=<redacted>" {
		t.Fatalf("query value kept the credential shape: %q", got)
	}
	for _, text := range []string{"Bearer abc", "ask-me later", "plain answer"} {
		if got := PrivacyString(text, nil); got != text {
			t.Fatalf("ordinary text %q changed to %q", text, got)
		}
	}
}
