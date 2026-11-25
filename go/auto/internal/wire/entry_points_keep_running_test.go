package wire

import (
	"fmt"
	"strings"
	"testing"
)

// uncheckableTexts are attribute texts the privacy filter cannot finish.
func uncheckableTexts() []string {
	return []string{
		strings.Repeat("[", 200) + strings.Repeat("]", 200),
		strings.Repeat(`{"a":`, 200) + `"api_key=sk-live-0123456789abcdef"` + strings.Repeat("}", 200),
		strings.Repeat("x", 9<<20),
	}
}

// equal compares two values by their printed form; fmt prints map keys in
// sorted order.
func equal(a, b any) bool { return fmt.Sprintf("%#v", a) == fmt.Sprintf("%#v", b) }

func ordinaryTexts() []string {
	return []string{
		"",
		"hello world",
		`{"api_key":"abc","note":"kept"}`,
		"Bearer abcdefghijklmnop0123 said hello",
		"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
		`[{"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"}}]`,
		"https://example.com/a?token=abc123def456&x=1",
	}
}

// returnsNormally reports a call that does not return normally as a failure
// of its own case, so every case is reported.
func returnsNormally(t *testing.T, call func()) {
	t.Helper()
	defer func() {
		if v := recover(); v != nil {
			t.Errorf("the call did not return normally: %v", v)
		}
	}()
	call()
}

func returns(call func()) (ok bool) {
	defer func() {
		if recover() != nil {
			ok = false
		}
	}()
	call()
	return true
}

func entryPoints(text string) map[string]func() any {
	same := func(s string) string { return s }
	return map[string]func() any{
		"PrivacyString":                    func() any { return PrivacyString(text, nil) },
		"PrivacyAttributeString":           func() any { return PrivacyAttributeString("input.value", text, nil) },
		"PrivacyAttributeStringWithMasker": func() any { return PrivacyAttributeStringWithMasker("input.value", text, same) },
		"PrivacyValue":                     func() any { return PrivacyValue(map[string]any{"input.value": text}, "", nil, nil) },
		"PrivacyValueWithMasker":           func() any { return PrivacyValueWithMasker(map[string]any{"input.value": text}, "", nil, same) },
		"PrivacyValueFailClosed": func() any {
			return PrivacyValueFailClosed(map[string]any{"input.value": text}, "", nil, same, MediaRisk{})
		},
	}
}

func filters(text string) map[string]func() any {
	same := func(s string) string { return s }
	return map[string]func() any{
		"PrivacyString":                    func() any { return privacyString(text, nil) },
		"PrivacyAttributeString":           func() any { return privacyAttributeString("input.value", text, nil) },
		"PrivacyAttributeStringWithMasker": func() any { return privacyAttributeStringWithMasker("input.value", text, same) },
		"PrivacyValue":                     func() any { return privacyValue(map[string]any{"input.value": text}, "", nil, nil) },
		"PrivacyValueWithMasker":           func() any { return privacyValueWithMasker(map[string]any{"input.value": text}, "", nil, same) },
		"PrivacyValueFailClosed": func() any {
			return privacyValueFailClosed(map[string]any{"input.value": text}, "", nil, same, MediaRisk{})
		},
	}
}

func TestEntryPointsReturnThePlaceholder(t *testing.T) {
	for i, text := range uncheckableTexts() {
		for name, call := range entryPoints(text) {
			t.Run(fmt.Sprintf("%s/text%d", name, i), func(t *testing.T) {
				returnsNormally(t, func() {
					if got := call(); got != CredentialRedacted {
						t.Errorf("got %.40v, want the placeholder", got)
					}
				})
			})
		}
	}
}

func TestEntryPointsKeepTheFilterOutput(t *testing.T) {
	for i, text := range ordinaryTexts() {
		want := filters(text)
		for name, call := range entryPoints(text) {
			t.Run(fmt.Sprintf("%s/text%d", name, i), func(t *testing.T) {
				if got, w := call(), want[name](); !equal(got, w) {
					t.Errorf("got %.200v, want %.200v", got, w)
				}
			})
		}
	}
}

func TestUnguardedFiltersLeaveTheFallbackToTheCaller(t *testing.T) {
	same := func(s string) string { return s }
	for i, text := range uncheckableTexts() {
		for name, call := range map[string]func(){
			"PrivacyString":                    func() { Unguarded.PrivacyString(text, nil) },
			"PrivacyAttributeStringWithMasker": func() { Unguarded.PrivacyAttributeStringWithMasker("input.value", text, same) },
			"PrivacyValueWithMasker":           func() { Unguarded.PrivacyValueWithMasker(map[string]any{"input.value": text}, "", nil, same) },
			"PrivacyValueFailClosed": func() {
				Unguarded.PrivacyValueFailClosed(map[string]any{"input.value": text}, "", nil, same, MediaRisk{})
			},
		} {
			t.Run(fmt.Sprintf("%s/text%d", name, i), func(t *testing.T) {
				if returns(call) {
					t.Error("the filter returned a value for a text it cannot check")
				}
			})
		}
	}
	for i, text := range ordinaryTexts() {
		t.Run(fmt.Sprintf("ordinary/text%d", i), func(t *testing.T) {
			if got, want := Unguarded.PrivacyString(text, nil), PrivacyString(text, nil); got != want {
				t.Errorf("got %q, want %q", got, want)
			}
			if got, want := Unguarded.PrivacyAttributeStringWithMasker("input.value", text, same), PrivacyAttributeStringWithMasker("input.value", text, same); got != want {
				t.Errorf("got %q, want %q", got, want)
			}
			value := map[string]any{"input.value": text, "mime_type": "image/png"}
			if got, want := Unguarded.PrivacyValueWithMasker(value, "", nil, same), PrivacyValueWithMasker(value, "", nil, same); !equal(got, want) {
				t.Errorf("got %v, want %v", got, want)
			}
			if got, want := Unguarded.PrivacyValueFailClosed(value, "", nil, same, MediaRisk{}), PrivacyValueFailClosed(value, "", nil, same, MediaRisk{}); !equal(got, want) {
				t.Errorf("got %v, want %v", got, want)
			}
		})
	}
}

func TestCapturedResultKeepsItsFallback(t *testing.T) {
	for i, text := range uncheckableTexts() {
		for _, field := range []string{"name", "description"} {
			t.Run(fmt.Sprintf("%s/text%d", field, i), func(t *testing.T) {
				r := &Result{Name: "chat m", Description: "kept status", OK: true, Attributes: map[string]any{
					"gen_ai.operation.name": "chat",
					"gen_ai.request.model":  "m",
					"input.value":           "hi",
					"input.mime_type":       "text/plain",
				}}
				if field == "name" {
					r.Name = text
				} else {
					r.Description = text
				}
				returnsNormally(t, func() { scrubResult(r, Request{}) })
				want := map[string]any{"traceai.capture.layer": "wire", "traceai.wire.privacy_degraded": true, "gen_ai.operation.name": "chat"}
				if r.Name != "traceai span" || r.Description != "" || !equal(r.Attributes, want) {
					t.Errorf("got %q %q %v", r.Name, r.Description, r.Attributes)
				}
			})
		}
	}
}
