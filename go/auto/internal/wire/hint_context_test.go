package wire

import (
	"fmt"
	"strings"
	"testing"
)

func TestS6MixedRepresentationMediaHints(t *testing.T) {
	const root = "gen_ai.input.messages.0.parts.0.source"
	const payload = "AQIDBA==" // No magic and below the generic base64 threshold.
	for _, mime := range []any{map[string]any{"mimeType": "image/png"}, `{"mimeType":"image/png"}`} {
		for _, data := range []any{payload, []any{payload}} {
			value := map[string]any{root: mime, root + ".data": data, "gen_ai.input.messages.0.parts.1.source.data": "safe neighbor"}
			got := PrivacyValue(value, "", nil, nil)
			if text := fmt.Sprint(got); strings.Contains(text, payload) || !strings.Contains(text, "safe neighbor") {
				t.Fatalf("mixed hint missed media or crossed numeric sibling: %v", got)
			}
		}
	}
	// A hint carried by an indexed native array also covers a flattened sibling.
	value := map[string]any{"parts": []any{map[string]any{"source": map[string]any{"mimeType": "image/png"}}}, "parts.0.source.data": payload, "parts.10.source.data": "safe neighbor"}
	if got := fmt.Sprint(PrivacyValue(value, "", nil, nil)); strings.Contains(got, payload) || !strings.Contains(got, "safe neighbor") {
		t.Fatal("indexed native hint did not protect equivalent flattened path")
	}
}

func TestS6ImageHintBudgetFailsClosed(t *testing.T) {
	cyclic := map[string]any{}
	cyclic["next"] = cyclic
	if got := HideImageReferences(cyclic, "hidden"); got != "hidden" {
		t.Fatal("cyclic hint indexing did not fail closed")
	}
	key := strings.Repeat("source.", privacyRules.Limits.Depth+1) + "url"
	got := HideImageReferences(map[string]any{key: "private reference"}, "hidden").(map[string]any)
	if got[key] != "hidden" {
		t.Fatal("over-deep flattened image path did not fail closed")
	}
}
