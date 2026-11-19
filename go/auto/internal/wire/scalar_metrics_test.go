package wire

import "testing"

// Catalog scalar metric keys are usage metadata, not audio payload, even though
// their names contain "audio" and "bytes". The role is closed: exact keys,
// valid values and no media context only.
func TestScalarMetricRolesKeepUsageCounts(t *testing.T) {
	check := func(key string, value, want any) {
		t.Helper()
		got := PrivacyValue(map[string]any{key: value}, "", nil, nil).(map[string]any)[key]
		if got != want {
			t.Errorf("%s: %v exported as %v, want %v", key, value, got, want)
		}
	}
	// Declared counters keep their values.
	check("gen_ai.usage.input_tokens.audio", int64(7), int64(7))
	check("gen_ai.usage.output_tokens.audio", float64(3), float64(3))
	check("gen_ai.usage.input_tokens.audio", "7", "7")
	check("gen_ai.usage.cache_read_tokens.audio", int64(2), int64(2))
	check("elevenlabs.audio.bytes", int64(4096), int64(4096))
	check("gen_ai.audio.bytes", "123", "123")
	// Invalid typed values, undeclared spellings and arrays keep the media policy.
	check("gen_ai.audio.bytes", "1234", "<omitted: 4 base64 chars>")
	check("elevenlabs.audio.bytes", "12", "<omitted: 2 base64 chars>")
	check("gen_ai.usage.cache_read_tokens.audio", int64(-1), "<omitted: media>")
	check("gen_ai.usage.cache_read_tokens.audio", float64(2.5), "<omitted: media>")
	check("usage.input_tokens.audio", int64(7), "<omitted: media>")
	check("gen_ai.usage.input_tokens.audio", []any{float64(1), float64(2)}, "<omitted: media>")
	// An explicit audio MIME on an ancestor wins over the metric role.
	attrs := map[string]any{"gen_ai.usage.input_tokens.audio": int64(7), "gen_ai.usage.mime_type": "audio/wav"}
	if got := PrivacyValue(attrs, "", nil, nil).(map[string]any)["gen_ai.usage.input_tokens.audio"]; got != "<omitted: media>" {
		t.Errorf("metric role ignored an audio MIME hint: %v", got)
	}
}
