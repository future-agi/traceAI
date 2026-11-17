package wire

import (
	"bytes"
	"compress/flate"
	"compress/gzip"
	"compress/zlib"
	"context"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"strings"
	"testing"
)

func TestResponsesRequiresEitherKey(t *testing.T) {
	u := "https://api.openai.com/v1/responses"
	for _, s := range []string{`{"model":"m","prompt":{"id":"pmpt_123"}}`, `{"input":"hi"}`} {
		if Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(s)}, Response{Status: 200}) == nil {
			t.Fatalf("either key should suffice: %s", s)
		}
	}
	if Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(`{"prompt":{}}`)}, Response{Status: 200}) != nil {
		t.Fatal("neither key should not match")
	}
}
func TestMultipartTruncationDropsSpan(t *testing.T) {
	ct := "multipart/form-data; boundary=b"
	data := "--b\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n" + strings.Repeat("x", 70000) + "\r\n--b\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n--b--\r\n"
	fields, files, trunc := ReadMultipart(strings.NewReader(data), ct, 4)
	u := "https://api.openai.com/v1/audio/transcriptions"
	r := Build(Find("POST", u), Request{Method: "POST", URL: u, Header: http.Header{"Content-Type": {ct}}, Fields: fields, Files: files, Truncated: trunc}, Response{Status: 200})
	if !trunc || fields["model"] != "whisper-1" || len(fields["prompt"]) != 65536 || r == nil || r.Attributes["traceai.wire.truncated"] != true {
		t.Fatalf("truncated multipart lost model/capture: truncated=%v fields=%v captured=%v", trunc, len(fields), r != nil)
	}
}
func TestMetadataExceedsMultipartBudget(t *testing.T) {
	var b bytes.Buffer
	w := multipart.NewWriter(&b)
	for i := 0; i < 128; i++ {
		_, _ = w.CreateFormFile("file", strings.Repeat("F", 256))
	}
	_ = w.Close()
	fields, files, trunc := ReadMultipart(&b, w.FormDataContentType(), 1)
	n := 0
	for _, f := range files {
		n += len(f.Filename) + len(f.Name) + len(f.ContentType)
	}
	if n > 1 || !trunc || len(fields) != 0 {
		t.Fatalf("metadata escaped budget: bytes=%d fields=%d truncated=%v", n, len(fields), trunc)
	}
}
func TestMultipartCardinalityAndEmptyFieldsBounded(t *testing.T) {
	var b bytes.Buffer
	w := multipart.NewWriter(&b)
	for i := 0; i < 1000; i++ {
		_ = w.WriteField(fmt.Sprintf("field%d", i), "")
	}
	_ = w.Close()
	fields, files, trunc := ReadMultipart(&b, w.FormDataContentType(), 1)
	if !trunc || len(fields) > 64 || len(files) != 0 {
		t.Fatalf("unbounded empty fields: %d", len(fields))
	}
}
func TestTruncatedAndUnavailableCatalogOnly(t *testing.T) {
	for _, host := range []string{"api.openai.com", "app.example"} {
		for _, mode := range []string{"unavailable", "truncated", "malformed"} {
			u := "https://" + host + "/v1/chat/completions"
			req := Request{Method: "POST", URL: u, Unavailable: mode == "unavailable", Truncated: mode == "truncated"}
			if mode == "malformed" {
				req.Body = []byte(`{"not_ai":true}`)
			}
			r := Build(Find("POST", u), req, Response{Status: 200})
			want := host == "api.openai.com" && mode != "malformed"
			if (r != nil) != want {
				t.Fatalf("%s/%s captured=%v", host, mode, r != nil)
			}
			if r != nil {
				if _, has := r.Attributes["input.value"]; has {
					t.Fatal("unavailable body retained content")
				}
			}
		}
	}
}
func TestRequestParametersExcludeEveryContentKey(t *testing.T) {
	b := object{"model": "m", "temperature": 0.5}
	for _, key := range contentKeys {
		b[key] = "PRIVATE"
	}
	p := parameters(b)
	if len(p) != 2 || p["model"] != "m" {
		t.Fatalf("content in parameters: %v", p)
	}
}
func TestPrivacyEveryInlineMediaShape(t *testing.T) {
	payload := strings.Repeat("YWJj", 100)
	for _, body := range []string{
		`{"messages":[{"content":[{"input_audio":{"data":"` + payload + `"}}]}]}`,
		`{"source":{"data":"` + payload + `"}}`,
		`{"inlineData":{"data":"` + payload + `"}}`,
		`{"source":{"bytes":"` + payload + `"}}`,
		`{"file":{"file_data":"` + payload + `"}}`,
		`{"audio_base64":"` + payload + `"}`,
		`{"b64_json":"` + payload + `"}`,
		`{"images":["` + payload + `"]}`,
		`prefix data:audio/wav;base64,` + payload + ` suffix`,
		`{"url":"data:image/png;base64,` + payload + `"}`,
	} {
		got := PrivacyString(body, nil)
		if strings.Contains(got, payload) || !strings.Contains(got, "<omitted: 400 base64 chars>") {
			t.Fatalf("media leaked/incorrect marker: %s", got)
		}
		if PrivacyString(got, nil) != got {
			t.Fatalf("privacy scrubbing is not idempotent: %s", got)
		}
	}
	if got := PrivacyString(`{"data":"short","text":"hello"}`, nil); got != `{"data":"short","text":"hello"}` {
		t.Fatalf("non-media text changed: %s", got)
	}
}
func TestPrivacyCredentialKeysSignedURLsAndNestedJSON(t *testing.T) {
	for _, key := range strings.Fields("authorization authorization_token api-key x_api_key xi-api-key api_token token access_token refresh_token id_token secret secret_key client_secret password private_key") {
		body := dump(object{"outer": []any{object{key: "PRIVATE"}}})
		got := PrivacyString(body, nil)
		if strings.Contains(got, "PRIVATE") || !strings.Contains(got, CredentialRedacted) {
			t.Fatalf("credential key missed %s: %s", key, got)
		}
	}
	for _, key := range strings.Fields("key api_key apikey token access_token sig signature X-Amz-Signature X-Amz-Credential X-Amz-Security-Token X-Goog-Signature X-Goog-Credential se sp sv sr skoid sktid") {
		url := "https://example.com/a?public=ok&" + key + "=PRIVATE&other=value"
		got := PrivacyString("error fetching "+url, nil)
		if strings.Contains(got, "PRIVATE") || !strings.Contains(got, key+"=<redacted>") || PrivacyString(got, nil) != got {
			t.Fatalf("signed URL missed: %s", got)
		}
	}
	body := dump(object{"arguments": `{"authorization_token":"PRIVATE"}`})
	if got := PrivacyString(body, nil); strings.Contains(got, "PRIVATE") {
		t.Fatalf("nested tool arguments leaked: %s", got)
	}
	if got := PrivacyString("bad FI_API_SECRET and tiny", []string{"FI_API_SECRET", "tiny"}); got != "bad <redacted> and tiny" {
		t.Fatalf("echo threshold: %s", got)
	}
}
func TestCompressedStreamsAndUnsupportedEncoding(t *testing.T) {
	plain := []byte("data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"partial\"}}]}\n\n")
	for _, encoding := range []string{"gzip", "deflate", "raw-deflate", "br", "zstd", "invalid-gzip"} {
		t.Run(encoding, func(t *testing.T) {
			var buf bytes.Buffer
			var w io.WriteCloser
			wireEncoding := encoding
			switch encoding {
			case "gzip":
				w = gzip.NewWriter(&buf)
			case "deflate":
				w = zlib.NewWriter(&buf)
			case "raw-deflate":
				w, _ = flate.NewWriter(&buf, flate.DefaultCompression)
				wireEncoding = "deflate"
			case "invalid-gzip":
				wireEncoding = "gzip"
			}
			if w != nil {
				_, _ = w.Write(plain)
				_ = w.Close()
			} else {
				buf.WriteString("NOT ENCODED")
			}
			u := "https://api.openai.com/v1/chat/completions"
			r := Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(`{"messages":[]}`)}, Response{Status: 200, Header: http.Header{"Content-Type": {"text/event-stream"}, "Content-Encoding": {wireEncoding}}, Body: buf.Bytes(), Error: context.DeadlineExceeded})
			if r == nil || r.OK || r.Attributes["error.type"] == nil {
				t.Fatal("midstream error lost")
			}
			if w != nil {
				if r.Attributes["output.value"] != "partial" {
					t.Fatal("compressed partial output lost")
				}
			} else if r.Attributes["traceai.wire.undecoded_encoding"] != wireEncoding || r.Attributes["output.value"] != nil {
				t.Fatal("unsupported encoding parsed or not marked")
			}
		})
	}
}
func TestCompressedRequestAndDecompressionCap(t *testing.T) {
	u := "https://api.openai.com/v1/chat/completions"
	var reqbuf bytes.Buffer
	w := gzip.NewWriter(&reqbuf)
	_, _ = w.Write([]byte(`{"model":"m","messages":[]}`))
	_ = w.Close()
	req := Request{Method: "POST", URL: u, Header: http.Header{"Content-Encoding": {"gzip"}}, Body: reqbuf.Bytes()}
	r := Build(Find("POST", u), req, Response{Status: 200})
	if r == nil || r.Attributes["gen_ai.request.model"] != "m" {
		t.Fatal("compressed request not decoded")
	}
	var buf bytes.Buffer
	w = gzip.NewWriter(&buf)
	_, _ = w.Write(bytes.Repeat([]byte("x"), 10000))
	_ = w.Close()
	req.MaxBodyBytes = 1024
	r = Build(Find("POST", u), req, Response{Status: 200, Header: http.Header{"Content-Encoding": {"gzip"}}, Body: buf.Bytes()})
	if r == nil || r.Attributes["traceai.wire.truncated"] != true || r.Attributes["output.value"] != nil {
		t.Fatal("decompressed body exceeded cap")
	}
}

func TestProviderHostPriorityBeatsPort(t *testing.T) {
	m := Find("POST", "https://api.deepgram.com:11434/v1/listen")
	if m == nil || m.Provider != "deepgram" {
		t.Fatal("earlier port rule overrode exact provider host")
	}
	if p := resolve("bedrock-runtime.bad.example", 443); p != "" {
		t.Fatalf("match-all rule bypassed: %s", p)
	}
}

func TestOpenAIImagesPreserveLengthMarker(t *testing.T) {
	u := "https://api.openai.com/v1/images/generations"
	payload := strings.Repeat("YWJj", 100)
	r := Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(`{"prompt":"draw"}`)}, Response{Status: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: []byte(`{"data":[{"b64_json":"` + payload + `"}]}`)})
	if r == nil || strings.Contains(str(r.Attributes["output.value"]), payload) || !strings.Contains(str(r.Attributes["output.value"]), "<omitted: 400 base64 chars>") {
		t.Fatalf("image length marker lost: %v", r)
	}
}

func TestTruncatedCompressionRetainsPartialOutput(t *testing.T) {
	plain := "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"partial\"}}]}\n\n"
	var buf bytes.Buffer
	w := gzip.NewWriter(&buf)
	_, _ = w.Write([]byte(plain))
	_ = w.Close()
	u := "https://api.openai.com/v1/chat/completions"
	r := Build(Find("POST", u), Request{Method: "POST", URL: u, Body: []byte(`{"messages":[]}`)}, Response{Status: 200, Header: http.Header{"Content-Type": {"text/event-stream"}, "Content-Encoding": {"gzip"}}, Body: buf.Bytes()[:buf.Len()-8], Error: context.DeadlineExceeded})
	if r == nil || r.OK || r.Attributes["output.value"] != "partial" || r.Attributes["traceai.wire.undecoded_encoding"] != "gzip" {
		t.Fatalf("partial compressed events lost: %v", r)
	}
}
