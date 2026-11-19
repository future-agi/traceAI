package wire

import "testing"

// Catalog image and video type or modality declarations make their object's
// payload fields media at any length, with no magic bytes. Image declarations
// also cover the image payload fields (image, image_url, url) for encoded
// values, while an ordinary HTTP URL stays text.
func TestImageAndVideoDeclarationsOmitPayloads(t *testing.T) {
	const short = "QUJDREVGR0g=" // 12 base64 characters with no media signature
	const marker = "<omitted: 12 base64 chars>"
	walk := func(value map[string]any) map[string]any {
		t.Helper()
		out, ok := PrivacyValue(value, "", nil, nil).(map[string]any)
		if !ok {
			t.Fatalf("privacy walk changed the object shape: %v", value)
		}
		return out
	}
	for _, kind := range []string{"image", "input_image", "image_url", "video", "input_video"} {
		if got := walk(map[string]any{"type": kind, "data": short})["data"]; got != marker {
			t.Errorf("type %s: data exported as %v", kind, got)
		}
	}
	if got := walk(map[string]any{"modality": "IMAGE", "data": short})["data"]; got != marker {
		t.Errorf("image modality: data exported as %v", got)
	}
	if got := walk(map[string]any{"modality": "video", "content": short})["content"]; got != marker {
		t.Errorf("video modality: content exported as %v", got)
	}
	got := walk(map[string]any{"type": "image", "image": short, "url": "https://example.invalid/cat.png"})
	if got["image"] != marker || got["url"] != "https://example.invalid/cat.png" {
		t.Errorf("image payload fields: %v", got)
	}
	nested := walk(map[string]any{"type": "image_url", "image_url": map[string]any{"url": short}})
	if url := nested["image_url"].(map[string]any)["url"]; url != marker {
		t.Errorf("image_url.url exported as %v", url)
	}
	// Video does not extend the image-only URL field, and other types change nothing.
	if got := walk(map[string]any{"type": "video", "url": short})["url"]; got != short {
		t.Errorf("video url changed to %v", got)
	}
	if got := walk(map[string]any{"type": "text", "data": short})["data"]; got != short {
		t.Errorf("text data changed to %v", got)
	}
}
